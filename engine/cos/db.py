"""SQLite access. Thin on purpose, the schema carries the design."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init() -> sqlite3.Connection:
    conn = connect()
    conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()
    return conn


# ── documents ──────────────────────────────────────────────────────────
def upsert_document(conn, *, source, source_id, ts, title, participants,
                    direction, body, meta, version: str | None = None) -> tuple[int, bool]:
    """Returns (document_id, is_new).

    `version` is a change-stamp (Notion's last_edited_time). A call page is
    created when the meeting starts and the transcript lands minutes later, so
    an unchanged-id check alone would permanently store the empty version.
    When the stamp moves we rewrite the body and clear processed_at so the
    extractor sees it again.
    """
    cur = conn.execute(
        "SELECT id, meta FROM documents WHERE source=? AND source_id=?", (source, source_id)
    )
    row = cur.fetchone()
    if row:
        if version:
            stored = (json.loads(row["meta"] or "{}") or {}).get("version")
            if stored != version:
                conn.execute(
                    """UPDATE documents SET body=?, title=?, meta=?, processed_at=NULL
                       WHERE id=?""",
                    (body, title, json.dumps({**meta, "version": version},
                                             ensure_ascii=False), row["id"]),
                )
                return row["id"], True          # counts as new work for the extractor
        return row["id"], False
    cur = conn.execute(
        """INSERT INTO documents
           (source, source_id, ts, title, participants, direction, body, meta, ingested_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (source, source_id, ts, title, json.dumps(participants, ensure_ascii=False),
         direction, body,
         json.dumps({**meta, "version": version} if version else meta, ensure_ascii=False),
         now()),
    )
    return cur.lastrowid, True


def unprocessed_documents(conn, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM documents WHERE processed_at IS NULL ORDER BY ts LIMIT ?", (limit,)
    ).fetchall()


def mark_processed(conn, doc_id: int) -> None:
    conn.execute("UPDATE documents SET processed_at=? WHERE id=?", (now(), doc_id))


# ── watermarks ─────────────────────────────────────────────────────────
def get_watermark(conn, source: str) -> str | None:
    row = conn.execute("SELECT last_ts FROM watermarks WHERE source=?", (source,)).fetchone()
    return row["last_ts"] if row else None


def set_watermark(conn, source: str, last_ts: str) -> None:
    conn.execute(
        """INSERT INTO watermarks(source, last_ts, updated_at) VALUES(?,?,?)
           ON CONFLICT(source) DO UPDATE SET last_ts=excluded.last_ts,
                                             updated_at=excluded.updated_at""",
        (source, last_ts, now()),
    )


# ── runs ───────────────────────────────────────────────────────────────
def start_run(conn, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs(started, kind) VALUES(?,?)", (now(), kind))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, stats: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE runs SET finished=?, stats=?, error=? WHERE id=?",
        (now(), json.dumps(stats or {}, ensure_ascii=False), error, run_id),
    )
    conn.commit()
