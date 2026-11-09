import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    api_key: str = field(default="", repr=False)
    chat_model: str = "gpt-4.1-mini"
    embedding_model: str = "text-embedding-3-small"
    max_upload_bytes: int = 10 * 1024 * 1024
    max_pages: int = 50
    max_client_chunks: int = 5000

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env")
        directory = Path(os.getenv("DATA_DIR", "data"))
        return cls(
            data_dir=(directory if directory.is_absolute() else ROOT / directory).resolve(),
            api_key=os.getenv("OPENAI_API_KEY", "").strip(),
            chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4.1-mini"),
            embedding_model=os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        )
