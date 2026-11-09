import io

from pypdf import PdfReader, PdfWriter

from app.config import Settings
from app.main import create_app
from fastapi.testclient import TestClient
from tests.conftest import create_client, pdf_bytes, upload


def test_pdf_upload_search_view_and_delete(api):
    client = create_client(api)
    pdf = pdf_bytes(["Northstar insurance invoice NS-1042.", "Period of insurance: 1 July 2026 to 30 June 2027."])
    response = upload(api, client, pdf, "insurance.pdf")
    assert response.status_code == 201
    document = response.json()["id"]
    assert response.json()["page_count"] == 2
    result = api.post(f"/api/clients/{client}/ask", json={"question": "What period does the insurance cover?"}).json()
    assert result["status"] == "search_only"
    assert result["claims"] == []
    assert result["sources"][0]["page"] == 2
    assert "30 June 2027" in result["sources"][0]["context"]
    page = api.get(f"/api/clients/{client}/documents/{document}/pages/2").json()
    assert "30 June 2027" in page["text"]
    original = api.get(f"/api/clients/{client}/documents/{document}/file")
    assert original.content == pdf
    assert original.headers["content-type"] == "application/pdf"
    assert api.delete(f"/api/clients/{client}/documents/{document}").status_code == 204
    assert api.get(f"/api/clients/{client}/documents/{document}/file").status_code == 404
    assert api.post(f"/api/clients/{client}/ask", json={"question": "insurance"}).json()["sources"] == []
    with api.app.state.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_client_scope_applies_to_search_files_pages_and_deletion(api):
    oak, other = create_client(api), create_client(api, "Other client")
    document = upload(api, oak, "Confidential Northstar invoice amount GBP 1200.").json()["id"]
    upload(api, other, "Office rent: GBP 400.")
    result = api.post(f"/api/clients/{other}/ask", json={"question": "Northstar"}).json()
    assert result["sources"] == []
    for suffix in ("file", "pages/1"):
        assert api.get(f"/api/clients/{other}/documents/{document}/{suffix}").status_code == 404
    assert api.delete(f"/api/clients/{other}/documents/{document}").status_code == 404
    assert api.get(f"/api/clients/{oak}/documents/{document}/file").status_code == 200


def test_duplicate_detection_is_scoped_to_client(api):
    first, second = create_client(api), create_client(api, "Second")
    content = "Supplier invoice: GBP 10.00."
    assert upload(api, first, content).status_code == 201
    assert upload(api, first, content, "renamed.txt").status_code == 409
    assert upload(api, second, content).status_code == 201


def test_invalid_and_partially_unreadable_documents_leave_no_index(api):
    client = create_client(api)
    cases = [(b"", "empty.txt"), (b"fake", "broken.pdf"), (b"hello", "file.exe"),
             (b"\xff\xfe", "bad.txt"), (pdf_bytes(["Invoice details", ""]), "mixed.pdf")]
    for content, filename in cases:
        assert upload(api, client, content, filename).status_code == 422
    assert api.get(f"/api/clients/{client}/documents").json() == []
    with api.app.state.database.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def test_encrypted_pdf_is_rejected(api):
    writer = PdfWriter()
    writer.append(PdfReader(io.BytesIO(pdf_bytes(["Insurance invoice"]))))
    writer.encrypt("example-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    client = create_client(api)
    response = upload(api, client, buffer.getvalue(), "protected.pdf")
    assert response.status_code == 422
    assert "password-protected" in response.json()["detail"]


def test_limits_and_validation(tmp_path):
    with TestClient(create_app(Settings(data_dir=tmp_path, max_upload_bytes=100, max_client_chunks=1))) as api:
        assert api.post("/api/clients", json={"name": "  "}).status_code == 422
        client = create_client(api)
        assert upload(api, client, "a" * 101).status_code == 413
        assert upload(api, client, "Insurance invoice").status_code == 201
        assert upload(api, client, "Another document").status_code == 422
        assert api.post(f"/api/clients/{client}/ask", json={"question": "  "}).status_code == 422
        assert api.get("/api/clients/missing/documents").status_code == 404


def test_persistence_and_client_deletion(tmp_path):
    settings = Settings(data_dir=tmp_path)
    with TestClient(create_app(settings)) as first:
        client = create_client(first)
        upload(first, client, "Annual insurance premium GBP 1200.")
    with TestClient(create_app(settings)) as second:
        assert len(second.get(f"/api/clients/{client}/documents").json()) == 1
        assert second.delete(f"/api/clients/{client}").status_code == 204
        assert second.get("/api/clients").json() == []
        with second.app.state.database.connect() as db:
            for table in ("documents", "pages", "chunks"):
                assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_local_browser_boundaries_and_static_page(api):
    assert api.get("/").status_code == 200
    assert "Find the answer." in api.get("/").text
    assert api.get("/static/app.js").status_code == 200
    assert "/api/clients/{client_id}/ask" in api.get("/openapi.json").json()["paths"]
    assert api.get("/api/status").json()["mode"] == "search_only"
    assert api.post("/api/clients", json={"name": "Unexpected"}, headers={"Origin": "https://other.example"}).status_code == 403
    assert api.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert api.get("/").headers["x-content-type-options"] == "nosniff"


def test_filename_does_not_control_storage_path(api):
    client = create_client(api)
    response = upload(api, client, "Invoice number 123.", "../../strange.txt")
    assert response.status_code == 201
    assert response.json()["name"] == "strange.txt"
