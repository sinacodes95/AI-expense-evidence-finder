"""Extract readable pages, preserving source boundaries for every chunk."""

import io
import re
from pathlib import Path

from pypdf import PdfReader


class DocumentError(ValueError):
    pass


def extract_pages(content: bytes, filename: str, max_pages: int = 50) -> tuple[list[str], str]:
    extension = Path(filename).suffix.lower()
    if extension == ".txt":
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise DocumentError("Text files must use UTF-8 encoding.") from exc
        if "\x00" in text:
            raise DocumentError("This file does not appear to be plain text.")
        pages, media_type = [text], "text/plain"
    elif extension == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise DocumentError("This file is not a valid PDF.")
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise DocumentError("Unlock this password-protected PDF before uploading it.")
            if len(reader.pages) > max_pages:
                raise DocumentError(f"Please upload PDFs with at most {max_pages} pages.")
            pages = [page.extract_text(extraction_mode="layout") or "" for page in reader.pages]
        except DocumentError:
            raise
        except Exception as exc:
            raise DocumentError("The PDF could not be read. Try exporting it again.") from exc
        media_type = "application/pdf"
    else:
        raise DocumentError("Upload a PDF or a UTF-8 .txt file.")
    pages = [re.sub(r"\n{3,}", "\n\n", page.replace("\x00", "")).strip() for page in pages]
    if not pages or not any(pages):
        raise DocumentError("No readable text found. Scanned PDFs need OCR before upload.")
    empty = [str(index + 1) for index, page in enumerate(pages) if not page.strip()]
    if empty:
        raise DocumentError(
            f"No readable text on page(s) {', '.join(empty)}. "
            "Apply OCR or remove blank pages, then upload again. Nothing was indexed."
        )
    if sum(map(len, pages)) > 500_000:
        raise DocumentError("Too much extracted text. Split this document into smaller files.")
    return pages, media_type


def chunk_page(text: str, size: int = 1600, overlap: int = 200) -> list[str]:
    """Prefer line boundaries and retain overlap; never mix different PDF pages."""
    result, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = text.rfind("\n", start + size // 2, end)
            if boundary == -1:
                boundary = text.rfind(" ", start + size // 2, end)
            if boundary != -1:
                end = boundary
        chunk = text[start:end].strip()
        if chunk:
            result.append(chunk)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result
