import hashlib
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import APIError
from pydantic import BaseModel, Field, field_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.config import Settings
from app.database import Database
from app.ingestion import DocumentError, chunk_page, extract_pages
from app.provider import OpenAIProvider
from app.rag import ask

STATIC = Path(__file__).parent / "static"


class NewClient(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("Enter a client name.")
        return value.strip()


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=1000)

    @field_validator("question")
    @classmethod
    def clean_question(cls, value):
        if len(value.strip()) < 3:
            raise ValueError("Enter a question with at least three characters.")
        return value.strip()


def create_app(settings: Settings | None = None, provider=None):
    settings = settings or Settings.from_env()
    database = Database(settings.data_dir)
    provider = provider or (OpenAIProvider(settings) if settings.api_key else None)
    app = FastAPI(title="AI Expense Evidence Finder", version="0.1.0", docs_url=None, redoc_url=None)
    app.state.database = database
    app.state.provider = provider
    # This is a localhost app, not a hosted multi-user service.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin:
            parsed = urlsplit(origin)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in {"http", "https"}:
                return JSONResponse({"detail": "Cross-origin writes are not allowed."}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        return response

    def require_client(client_id):
        with database.connect() as db:
            row = db.execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "Client not found.")
        return dict(row)

    def require_document(client_id, document_id):
        with database.connect() as db:
            row = db.execute("SELECT * FROM documents WHERE id=? AND client_id=?", (document_id, client_id)).fetchone()
        if row is None:
            raise HTTPException(404, "Document not found for this client.")
        return dict(row)

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/status")
    def status():
        return {"mode": "rag" if provider else "search_only", "max_upload_mb": settings.max_upload_bytes // (1024 * 1024),
                "max_pages": settings.max_pages}

    @app.get("/api/clients")
    def clients():
        with database.connect() as db:
            rows = db.execute("SELECT id, name FROM clients ORDER BY created_at, name").fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/clients", status_code=201)
    def new_client(body: NewClient):
        client_id = uuid.uuid4().hex
        with database.connect() as db:
            db.execute("INSERT INTO clients (id, name) VALUES (?, ?)", (client_id, body.name))
        return {"id": client_id, "name": body.name}

    @app.delete("/api/clients/{client_id}", status_code=204)
    def delete_client(client_id: str):
        require_client(client_id)
        with database.connect() as db:
            db.execute("DELETE FROM clients WHERE id=?", (client_id,))
        return Response(status_code=204)

    @app.get("/api/clients/{client_id}/documents")
    def documents(client_id: str):
        require_client(client_id)
        with database.connect() as db:
            rows = db.execute("""
                SELECT d.id, d.name, d.page_count, d.media_type, d.created_at,
                       COUNT(c.id) AS chunk_count
                FROM documents d LEFT JOIN chunks c ON c.document_id=d.id
                WHERE d.client_id=? GROUP BY d.id ORDER BY d.created_at DESC, d.name
            """, (client_id,)).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/clients/{client_id}/documents", status_code=201)
    def upload(client_id: str, file: UploadFile = File(...)):
        require_client(client_id)
        name = Path((file.filename or "document").replace("\\", "/")).name[:160]
        try:
            content = file.file.read(settings.max_upload_bytes + 1)
        finally:
            file.file.close()
        if len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "File exceeds the 10 MB upload limit.")
        if not content:
            raise HTTPException(422, "The uploaded file is empty.")
        digest = hashlib.sha256(content).hexdigest()
        with database.connect() as db:
            duplicate = db.execute("SELECT id FROM documents WHERE client_id=? AND content_hash=?", (client_id, digest)).fetchone()
        if duplicate:
            raise HTTPException(409, "This document is already in the client's workspace.")
        try:
            pages, media_type = extract_pages(content, name, settings.max_pages)
        except DocumentError as exc:
            raise HTTPException(422, str(exc)) from exc
        document_id = uuid.uuid4().hex
        chunks = [(uuid.uuid4().hex, document_id, page_number, chunk)
                  for page_number, text in enumerate(pages, start=1) for chunk in chunk_page(text)]
        try:
            with database.connect() as db:
                # Serialise the capacity check with insertion, including concurrent uploads.
                db.execute("BEGIN IMMEDIATE")
                count = db.execute("SELECT COUNT(*) FROM chunks c JOIN documents d ON c.document_id=d.id WHERE d.client_id=?", (client_id,)).fetchone()[0]
                if count + len(chunks) > settings.max_client_chunks:
                    raise HTTPException(422, "This client's local index is full. Remove unused documents first.")
                db.execute("INSERT INTO documents (id, client_id, name, content_hash, media_type, page_count, content) VALUES (?, ?, ?, ?, ?, ?, ?)",
                           (document_id, client_id, name, digest, media_type, len(pages), content))
                db.executemany("INSERT INTO pages (document_id, number, text) VALUES (?, ?, ?)",
                               [(document_id, i, text) for i, text in enumerate(pages, start=1)])
                db.executemany("INSERT INTO chunks (id, document_id, page, text) VALUES (?, ?, ?, ?)", chunks)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "The document was already uploaded or the client was removed. Refresh and try again.") from exc
        return {"id": document_id, "name": name, "page_count": len(pages), "chunk_count": len(chunks)}

    @app.delete("/api/clients/{client_id}/documents/{document_id}", status_code=204)
    def delete_document(client_id: str, document_id: str):
        require_document(client_id, document_id)
        with database.connect() as db:
            db.execute("DELETE FROM documents WHERE id=? AND client_id=?", (document_id, client_id))
        return Response(status_code=204)

    @app.get("/api/clients/{client_id}/documents/{document_id}/file")
    def original(client_id: str, document_id: str):
        document = require_document(client_id, document_id)
        return Response(document["content"], media_type=document["media_type"],
                        headers={"Content-Disposition": f"inline; filename*=UTF-8''{quote(document['name'], safe='')}"})

    @app.get("/api/clients/{client_id}/documents/{document_id}/pages/{page}")
    def page_text(client_id: str, document_id: str, page: int):
        document = require_document(client_id, document_id)
        with database.connect() as db:
            row = db.execute("SELECT text FROM pages WHERE document_id=? AND number=?", (document_id, page)).fetchone()
        if row is None:
            raise HTTPException(404, "Page not found.")
        return {"text": row["text"], "page": page, "page_count": document["page_count"],
                "filename": document["name"], "media_type": document["media_type"]}

    @app.post("/api/clients/{client_id}/ask")
    def answer(client_id: str, body: Question):
        require_client(client_id)
        try:
            return ask(database, client_id, body.question, provider, settings.embedding_model)
        except APIError as exc:
            # Never return provider exceptions: they can contain sensitive request data.
            raise HTTPException(503, "The AI service could not complete the request. Check your API key, billing, and connection, then retry.") from exc
        except (ValueError, IndexError) as exc:
            raise HTTPException(502, "The AI response could not be processed. Try again or ask a more specific question.") from exc

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
