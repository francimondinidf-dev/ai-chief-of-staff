"""Entity resolution: surface form -> canonical entity.

This is what makes `/cos do 3` work without you re-explaining anything, and it
is the thing your corrections teach. Deliberately simple: exact alias match on a
normalized surface form. Fuzzy guessing here would create wrong links that are
worse than no link, because a wrong link silently poisons a task's dossier.
"""
import re
import unicodedata

from . import db

_LEGAL = re.compile(
    r"\b(s\.?a\.?s\.?|s\.?a\.?r\.?l\.?|gmbh|ltd|limited|inc|llc|b\.?v\.?|"
    r"a/s|ab|oy|plc|corp|corporation|co|company|group|holding|holdings|"
    r"technologies|technology|labs|sa|spa|srl|nv)\b",
    re.I,
)
_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize(name: str) -> str:
    """'Acme Robotics, S.A.' and 'acme  robotics' collapse together."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = _PUNCT.sub(" ", s)
    s = _LEGAL.sub(" ", s)
    return _SPACE.sub(" ", s).strip()


def resolve(conn, name: str, kind: str = "company") -> int | None:
    """Return entity id, creating it if this surface form is genuinely new."""
    surface = normalize(name)
    if not surface:
        return None

    row = conn.execute(
        """SELECT a.entity_id FROM aliases a
           JOIN entities e ON e.id = a.entity_id
           WHERE a.surface = ? AND e.kind = ?""",
        (surface, kind),
    ).fetchone()
    if row:
        return row["entity_id"]

    row = conn.execute(
        "SELECT id FROM entities WHERE kind=? AND canonical_name=?", (kind, name.strip())
    ).fetchone()
    if row:
        entity_id = row["id"]
    else:
        cur = conn.execute(
            "INSERT INTO entities(kind, canonical_name, created_at) VALUES(?,?,?)",
            (kind, name.strip(), db.now()),
        )
        entity_id = cur.lastrowid

    conn.execute(
        """INSERT OR IGNORE INTO aliases(surface, entity_id, origin, created_at)
           VALUES(?,?,?,?)""",
        (surface, entity_id, "auto", db.now()),
    )
    return entity_id


def add_alias(conn, surface: str, entity_id: int, origin: str = "correction") -> None:
    conn.execute(
        """INSERT OR IGNORE INTO aliases(surface, entity_id, origin, created_at)
           VALUES(?,?,?,?)""",
        (normalize(surface), entity_id, origin, db.now()),
    )


def merge(conn, from_id: int, into_id: int) -> None:
    """Fold one entity into another and repoint everything. Corrections apply
    backwards, not just forwards, otherwise you re-correct the same company."""
    conn.execute("UPDATE OR IGNORE aliases SET entity_id=? WHERE entity_id=?", (into_id, from_id))
    conn.execute("UPDATE OR IGNORE task_entities SET entity_id=? WHERE entity_id=?", (into_id, from_id))
    conn.execute("DELETE FROM entities WHERE id=?", (from_id,))


def candidates(conn, name: str, kind: str = "company") -> list:
    return conn.execute(
        """SELECT DISTINCT e.* FROM entities e
           JOIN aliases a ON a.entity_id = e.id
           WHERE a.surface LIKE ? AND e.kind = ?""",
        (f"%{normalize(name)}%", kind),
    ).fetchall()
