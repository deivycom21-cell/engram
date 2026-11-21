"""Process lifecycle: one engine per process, health checks ("doctor"), graceful shutdown."""
from __future__ import annotations

import importlib.util
import logging
import sqlite3
import sys
import threading

from engram.core.config import Settings, load_settings
from engram.core.engine import EngramEngine, disk_free_mb

log = logging.getLogger("engram.lifecycle")

_engine: EngramEngine | None = None
_lock = threading.Lock()


def get_engine(settings: Settings | None = None) -> EngramEngine:
    global _engine
    with _lock:
        if _engine is None:
            _engine = EngramEngine(settings or load_settings())
        return _engine


def set_engine(engine: EngramEngine | None) -> None:
    global _engine
    with _lock:
        _engine = engine


def shutdown() -> None:
    global _engine
    with _lock:
        if _engine is not None:
            _engine.close()
            _engine = None


def doctor(settings: Settings | None = None) -> list[tuple[str, bool, str]]:
    """Environment checks with actionable messages. Does not modify anything except creating the data dir."""
    s = settings or load_settings()
    checks: list[tuple[str, bool, str]] = []
    checks.append(("python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0]))
    con = sqlite3.connect(":memory:")
    try:
        con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        fts = True
    except sqlite3.OperationalError:
        fts = False
    checks.append(("SQLite FTS5", fts, sqlite3.sqlite_version))
    vec = importlib.util.find_spec("sqlite_vec") is not None
    checks.append(("sqlite-vec (optional)", vec, "installed" if vec else "missing → numpy brute-force search"))
    ort = importlib.util.find_spec("onnxruntime") is not None and importlib.util.find_spec("tokenizers") is not None
    checks.append(("onnxruntime + tokenizers (optional)", ort, "installed" if ort else "pip install -e .[neural]"))
    from engram.memory.embeddings import onnx_model_available

    model = onnx_model_available(s.models_dir, s.onnx_model)
    checks.append((f"embedding model {s.onnx_model}", model, "present" if model else "run: engram models download"))
    s.ensure_dirs()
    free = disk_free_mb(s.data_dir)
    checks.append(("free disk space", free > 500, f"{free} MB at {s.data_dir}"))
    checks.append(("external LLM disabled", not s.allow_external_llm, "privacy: no data leaves this machine"
                   if not s.allow_external_llm else "allow_external_llm = true (Claude CLI may receive evidence)"))
    return checks
