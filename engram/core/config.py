"""Configuration.

Resolution order (later wins): defaults -> ``<data_dir>/engram.toml`` -> environment
variables (``ENGRAM_*``) -> explicit keyword overrides.

Default data directory:
* ``ENGRAM_DATA_DIR`` if set;
* ``<repo>/data`` when running from a source checkout (a ``pyproject.toml`` sits next to
  the package), so a cloned repo keeps its memory next to the code (git-ignored);
* ``~/.engram`` otherwise (pip install).
"""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

EmbeddingProviderName = Literal["auto", "onnx", "ollama", "hash", "none"]
LLMProviderName = Literal["none", "ollama", "claude-cli"]


def default_data_dir() -> Path:
    env = os.environ.get("ENGRAM_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    repo_root = Path(__file__).resolve().parents[2]
    if (repo_root / "pyproject.toml").is_file() and (repo_root / "engram").is_dir():
        return repo_root / "data"
    return Path.home() / ".engram"


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=default_data_dir)

    # --- ingestion -------------------------------------------------------------
    max_file_bytes: int = 25 * 1024 * 1024
    allowed_extensions: tuple[str, ...] = (
        ".pdf", ".md", ".markdown", ".txt", ".text", ".rst", ".org", ".log",
    )
    chunk_target_chars: int = 900
    chunk_max_chars: int = 1600
    chunk_min_chars: int = 40
    max_pdf_pages: int = 2000

    # --- embeddings ------------------------------------------------------------
    embedding_provider: EmbeddingProviderName = "auto"
    onnx_model: str = "multilingual-e5-small"
    models_path: Path | None = None   # share downloaded models between data dirs (ENGRAM_MODELS_DIR)
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_embed_model: str = "nomic-embed-text"
    embed_batch_size: int = 16
    onnx_threads: int = 2

    # --- associations / consolidation -----------------------------------------
    association_top_k: int = 5
    # Similarity thresholds are provider specific (see memory/embeddings.py);
    # ``None`` means "use the provider's calibrated default".
    association_threshold: float | None = None
    duplicate_threshold: float | None = None

    # --- optional language model (never required) -------------------------------
    llm_provider: LLMProviderName = "none"
    ollama_llm_model: str = "llama3.2:3b"
    # Sending memories to a remote service (Claude CLI) requires explicit consent.
    allow_external_llm: bool = False

    # --- server ----------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 7777

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engram.db"

    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def models_dir(self) -> Path:
        return Path(self.models_path) if self.models_path else self.data_dir / "models"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    def ensure_dirs(self) -> None:
        for d in (self.data_dir, self.sources_dir, self.models_dir, self.backups_dir, self.exports_dir):
            d.mkdir(parents=True, exist_ok=True)


_ENV_MAP = {
    "ENGRAM_EMBEDDINGS": "embedding_provider",
    "ENGRAM_LLM": "llm_provider",
    "ENGRAM_ALLOW_EXTERNAL_LLM": "allow_external_llm",
    "ENGRAM_HOST": "host",
    "ENGRAM_PORT": "port",
    "ENGRAM_MAX_FILE_MB": "max_file_mb",
    "ENGRAM_OLLAMA_URL": "ollama_url",
    "ENGRAM_MODELS_DIR": "models_path",
    "ENGRAM_OLLAMA_EMBED_MODEL": "ollama_embed_model",
    "ENGRAM_OLLAMA_LLM_MODEL": "ollama_llm_model",
}


def load_settings(**overrides) -> Settings:
    data_dir = Path(overrides.pop("data_dir", None) or default_data_dir()).expanduser().resolve()
    values: dict = {}
    cfg_file = data_dir / "engram.toml"
    if cfg_file.is_file():
        with cfg_file.open("rb") as fh:
            values.update(tomllib.load(fh))
    for env_key, field in _ENV_MAP.items():
        if env_key in os.environ:
            values[field] = os.environ[env_key]
    if "max_file_mb" in values:
        values["max_file_bytes"] = int(float(values.pop("max_file_mb")) * 1024 * 1024)
    if isinstance(values.get("allow_external_llm"), str):
        values["allow_external_llm"] = values["allow_external_llm"].lower() in ("1", "true", "yes")
    values.update({k: v for k, v in overrides.items() if v is not None})
    values["data_dir"] = data_dir
    return Settings(**values)
