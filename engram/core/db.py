"""SQLite access layer: connection management and reproducible, versioned schema.

A single connection is shared and every access is serialised with a re-entrant lock.
For a personal, single-user memory this is simpler and safer than a pool, and WAL
mode keeps reads cheap. Migrations are append-only: never edit an applied one.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("engram.db")

MIGRATIONS: list[str] = [
    # ---- v1: core schema ---------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS meta (
        key   TEXT PRIMARY KEY,
        value TEXT
    );

    CREATE TABLE documents (
        id                  INTEGER PRIMARY KEY,
        uid                 TEXT NOT NULL UNIQUE,
        title               TEXT NOT NULL,
        filename            TEXT NOT NULL,
        original_path       TEXT,
        stored_path         TEXT NOT NULL,
        sha256              TEXT NOT NULL UNIQUE,
        kind                TEXT NOT NULL,
        size_bytes          INTEGER NOT NULL,
        authored_at         TEXT,
        authored_at_source  TEXT,
        file_modified_at    TEXT,
        ingested_at         TEXT NOT NULL,
        previous_version_id INTEGER REFERENCES documents(id),
        metadata            TEXT NOT NULL DEFAULT '{}',
        status              TEXT NOT NULL DEFAULT 'active'
    );
    CREATE INDEX idx_documents_filename ON documents(filename);

    CREATE TABLE engrams (
        id                  INTEGER PRIMARY KEY,
        uid                 TEXT NOT NULL UNIQUE,
        document_id         INTEGER REFERENCES documents(id),
        kind                TEXT NOT NULL,
        kind_confidence     REAL,
        kind_reason         TEXT,
        title               TEXT,
        content             TEXT NOT NULL,
        content_hash        TEXT NOT NULL,
        position            INTEGER,
        page                INTEGER,
        char_start          INTEGER,
        char_end            INTEGER,
        occurred_at         TEXT,
        occurred_at_source  TEXT,
        created_at          TEXT,
        created_at_source   TEXT,
        ingested_at         TEXT NOT NULL,
        confidence          REAL,
        metadata            TEXT NOT NULL DEFAULT '{}',
        access_count        INTEGER NOT NULL DEFAULT 0,
        last_accessed_at    TEXT,
        status              TEXT NOT NULL DEFAULT 'active'
    );
    CREATE INDEX idx_engrams_doc ON engrams(document_id, position);
    CREATE INDEX idx_engrams_kind ON engrams(kind);
    CREATE INDEX idx_engrams_hash ON engrams(content_hash);
    CREATE INDEX idx_engrams_dates ON engrams(occurred_at, created_at);

    CREATE VIRTUAL TABLE engrams_fts USING fts5(
        content, title,
        content='engrams', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER engrams_ai AFTER INSERT ON engrams BEGIN
        INSERT INTO engrams_fts(rowid, content, title) VALUES (new.id, new.content, coalesce(new.title, ''));
    END;
    CREATE TRIGGER engrams_ad AFTER DELETE ON engrams BEGIN
        INSERT INTO engrams_fts(engrams_fts, rowid, content, title) VALUES ('delete', old.id, old.content, coalesce(old.title, ''));
    END;
    CREATE TRIGGER engrams_au AFTER UPDATE OF content, title ON engrams BEGIN
        INSERT INTO engrams_fts(engrams_fts, rowid, content, title) VALUES ('delete', old.id, old.content, coalesce(old.title, ''));
        INSERT INTO engrams_fts(rowid, content, title) VALUES (new.id, new.content, coalesce(new.title, ''));
    END;

    CREATE TABLE embeddings (
        engram_id INTEGER NOT NULL REFERENCES engrams(id),
        model     TEXT NOT NULL,
        dim       INTEGER NOT NULL,
        vector    BLOB NOT NULL,
        PRIMARY KEY (engram_id, model)
    );

    CREATE TABLE concepts (
        id    INTEGER PRIMARY KEY,
        name  TEXT NOT NULL,
        norm  TEXT NOT NULL,
        kind  TEXT NOT NULL,           -- concept | entity | tag
        UNIQUE (norm, kind)
    );
    CREATE TABLE engram_concepts (
        engram_id  INTEGER NOT NULL REFERENCES engrams(id),
        concept_id INTEGER NOT NULL REFERENCES concepts(id),
        weight     REAL NOT NULL DEFAULT 1.0,
        origin     TEXT NOT NULL,      -- extracted | explicit
        PRIMARY KEY (engram_id, concept_id)
    );
    CREATE INDEX idx_ec_concept ON engram_concepts(concept_id);

    CREATE TABLE relations (
        id          INTEGER PRIMARY KEY,
        source_id   INTEGER NOT NULL REFERENCES engrams(id),
        target_id   INTEGER NOT NULL REFERENCES engrams(id),
        type        TEXT NOT NULL,
        origin      TEXT NOT NULL CHECK (origin IN ('explicit', 'structural', 'inferred')),
        explanation TEXT NOT NULL,
        confidence  REAL NOT NULL DEFAULT 1.0,
        evidence    TEXT NOT NULL DEFAULT '{}',
        detector    TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'proposed',   -- proposed | confirmed | rejected
        created_at  TEXT NOT NULL,
        UNIQUE (source_id, target_id, type)
    );
    CREATE INDEX idx_rel_source ON relations(source_id);
    CREATE INDEX idx_rel_target ON relations(target_id);
    CREATE INDEX idx_rel_type ON relations(type);

    CREATE TABLE insights (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        title       TEXT NOT NULL,
        explanation TEXT NOT NULL,
        engram_ids  TEXT NOT NULL,
        score       REAL NOT NULL DEFAULT 0,
        status      TEXT NOT NULL DEFAULT 'open',      -- open | accepted | dismissed
        fingerprint TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE jobs (
        id         INTEGER PRIMARY KEY,
        kind       TEXT NOT NULL,
        status     TEXT NOT NULL,                     -- queued | running | done | failed
        payload    TEXT NOT NULL DEFAULT '{}',
        result     TEXT,
        error      TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE audit_log (
        id     INTEGER PRIMARY KEY,
        at     TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT NOT NULL DEFAULT '{}'
    );
    """,
]


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = NORMAL")
        self.vec_available = self._load_sqlite_vec()
        self.migrate()

    # ------------------------------------------------------------------ setup
    def _load_sqlite_vec(self) -> bool:
        try:
            import sqlite_vec  # type: ignore

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            return True
        except Exception as exc:  # missing package or sqlite built without extensions
            log.info("sqlite-vec unavailable, using numpy vector search (%s)", exc)
            return False

    def migrate(self) -> None:
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            current = int(row[0]) if row else 0
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                log.info("applying migration v%d", version)
                self.conn.execute("BEGIN")
                try:
                    for statement in _split_sql(script):
                        self.conn.execute(statement)
                    self.conn.execute(
                        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (str(version),),
                    )
                    self.conn.execute("COMMIT")
                except Exception:
                    self.conn.execute("ROLLBACK")
                    raise

    @property
    def schema_version(self) -> int:
        row = self.query_one("SELECT value FROM meta WHERE key='schema_version'")
        return int(row["value"]) if row else 0

    # ------------------------------------------------------------------ helpers
    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.conn.execute("BEGIN")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def execute(self, sql: str, params: Any = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, params)

    def query(self, sql: str, params: Any = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def query_one(self, sql: str, params: Any = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def get_meta(self, key: str) -> str | None:
        row = self.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def backup_to(self, dest: Path) -> None:
        with self.lock:
            target = sqlite3.connect(str(dest))
            try:
                self.conn.backup(target)
            finally:
                target.close()

    def close(self) -> None:
        with self.lock:
            try:
                self.conn.close()
            except Exception:
                pass


def _split_sql(script: str) -> list[str]:
    """Split a migration into statements, keeping trigger bodies intact."""
    statements, buf, depth = [], [], 0
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            depth += 1
        if depth and upper.startswith("END;"):
            depth -= 1
            statements.append("\n".join(buf))
            buf = []
            continue
        if not depth and stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
