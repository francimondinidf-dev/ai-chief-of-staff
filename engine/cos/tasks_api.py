"""Read and act on individual commitments, the half the daily brief can't do.

The brief is a broadcast: everything, once a day, one line each. This module is
the opposite end, pull ONE task and everything the database knows about it, so
a session can start work with zero re-explaining, then write the outcome back.

Reference resolution is the whole ergonomic point. You say "63", "the Acme
press release", or "#85" and mean the same kind of thing: the item you read in
this morning's email. Numbers you see there are BRIEF numbers, not task ids, and
the two ranges overlap, so a bare integer is a brief number first and a task id
only as a fallback, and both paths say out loud which one they took. Guessing
silently between two live tasks is the one failure that would make this
untrustworthy.
"""
from datetime import date, datetime

from . import db, entities

OPEN = ("open", "likely_done")
_ACTIVE = "t.status IN ('open','likely_done','snoozed')"


# ── helpers ────────────────────────────────────────────────────────────
def _brief_num(conn, task_id: int) -> int | None:
    row = conn.execute(
        "SELECT num FROM brief_items WHERE task_id=? ORDER BY brief_id DESC LIMIT 1",
        (task_id,)).fetchone()
    return row["num"] if row else None


def brief_numbers(conn) -> dict:
    """task_id -> most recent brief number. One query, not one per task."""
    out = {}
    for r in conn.execute("SELECT task_id, num FROM brief_items ORDER BY brief_id ASC"):
        out[r["task_id"]] = r["num"]
    return out


def _entity_names(conn, task_id: int) -> list:
    return [r["canonical_name"] for r in conn.execute(
        """SELECT e.canonical_name FROM task_entities te
           JOIN entities e ON e.id = te.entity_id
           WHERE te.task_id=? ORDER BY e.kind, e.canonical_name""", (task_id,))]


def age_days(row) -> int | None:
    try:
        return (date.today() - datetime.fromisoformat(row["first_seen"]).date()).days
    except Exception:
        return None


def due_flag(row) -> str:
    if not row["due"]:
        return ""
    overdue = row["due"] < date.today().isoformat()
    return f"  [{'OVERDUE ' if overdue else 'due '}{row['due']}]"


# ── listing ────────────────────────────────────────────────────────────
def active(conn, kind: str | None = None, include_snoozed: bool = False) -> list:
    sql = f"SELECT t.* FROM tasks t WHERE {_ACTIVE}"
    args: list = []
    if not include_snoozed:
        sql += " AND (t.snooze_until IS NULL OR t.snooze_until <= ?)"
        args.append(date.today().isoformat())
    if kind:
        sql += " AND t.kind=?"
        args.append(kind)
    sql += " ORDER BY (t.due IS NULL), t.due, t.id"
    return conn.execute(sql, args).fetchall()


# ── reference resolution ───────────────────────────────────────────────
class Ambiguous(Exception):
    def __init__(self, candidates):
        self.candidates = candidates
        super().__init__("ambiguous reference")


class NotFound(Exception):
    pass


def _score(conn, row, needle: str) -> float:
    """Cheap lexical + entity scoring. No LLM: resolution must be instant and
    explainable, and a wrong fuzzy hit is worse than asking."""
    n = needle.lower().strip()
    if not n:
        return 0.0
    title = (row["title"] or "").lower()
    detail = (row["detail"] or "").lower()
    who = f"{row['promiser'] or ''} {row['promisee'] or ''}".lower()
    s = 0.0
    if n == title:
        s += 100
    if n in title:
        s += 40
    if n in detail:
        s += 10
    if n in who:
        s += 8

    words = [w for w in n.replace("/", " ").split() if len(w) > 2]
    if words:
        s += 22 * (sum(1 for w in words if w in title) / len(words))
        s += 6 * (sum(1 for w in words if w in detail) / len(words))

    for e in _entity_names(conn, row["id"]):
        el = e.lower()
        if el and (el in n or n in el):
            s += 30
        else:
            norm = entities.normalize(e)
            if norm and norm in entities.normalize(n):
                s += 25
    return s


def resolve(conn, ref: str, include_closed: bool = False):
    """Return (task_row, how_it_was_resolved). Raises Ambiguous / NotFound."""
    ref = (ref or "").strip()
    if not ref:
        raise NotFound("empty reference")

    if ref.startswith("#") and ref[1:].isdigit():
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (int(ref[1:]),)).fetchone()
        if not row:
            raise NotFound(f"no task with id {ref[1:]}")
        return row, f"task id {row['id']}"

    # a bare integer is what the user read in the email: a brief number.
    if ref.isdigit():
        n = int(ref)
        row = conn.execute(
            """SELECT t.* FROM brief_items bi JOIN tasks t ON t.id=bi.task_id
               WHERE bi.num=? ORDER BY bi.brief_id DESC LIMIT 1""", (n,)).fetchone()
        if row and (include_closed or row["status"] in OPEN + ("snoozed",)):
            return row, f"brief #{n} -> task id {row['id']}"
        by_id = conn.execute("SELECT * FROM tasks WHERE id=?", (n,)).fetchone()
        if by_id and (include_closed or by_id["status"] in OPEN + ("snoozed",)):
            why = "no such brief number" if not row else f"brief #{n} is {row['status']}"
            return by_id, f"{why}; matched task id {n}"
        if row:
            return row, f"brief #{n} -> task id {row['id']} (status {row['status']})"
        if by_id:
            return by_id, f"task id {n} (status {by_id['status']})"
        raise NotFound(f"no brief item or task numbered {n}")

    pool = conn.execute(
        "SELECT * FROM tasks t" if include_closed
        else f"SELECT * FROM tasks t WHERE {_ACTIVE}").fetchall()
    scored = sorted(((_score(conn, r, ref), r) for r in pool), key=lambda p: -p[0])
    scored = [(s, r) for s, r in scored if s >= 12]
    if not scored:
        raise NotFound(f"nothing active matches {ref!r}")
    if len(scored) > 1 and scored[1][0] >= scored[0][0] * 0.8:
        raise Ambiguous([r for _, r in scored[:5]])
    return scored[0][1], f"text match (score {scored[0][0]:.0f}) -> task id {scored[0][1]['id']}"


# ── dossier ────────────────────────────────────────────────────────────
def dossier(conn, task_id: int, body_chars: int = 1500, related: bool = True) -> dict:
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not t:
        raise NotFound(f"no task {task_id}")

    ev = conn.execute(
        """SELECT e.role, e.quote, d.id AS doc_id, d.title, d.source, d.ts,
                  d.participants, d.direction, substr(d.body,1,?) AS body,
                  length(d.body) AS body_len
           FROM task_evidence e JOIN documents d ON d.id = e.document_id
           WHERE e.task_id=? ORDER BY (e.role<>'origin'), d.ts""",
        (body_chars, task_id)).fetchall()

    ents = conn.execute(
        """SELECT e.id, e.kind, e.canonical_name, e.domain, e.affinity_id
           FROM task_entities te JOIN entities e ON e.id=te.entity_id
           WHERE te.task_id=? ORDER BY e.kind, e.canonical_name""", (task_id,)).fetchall()

    siblings = []
    if related and ents:
        ids = [e["id"] for e in ents]
        q = ",".join("?" * len(ids))
        siblings = conn.execute(
            f"""SELECT DISTINCT t.id, t.kind, t.status, t.title, t.due, t.promiser
                FROM task_entities te JOIN tasks t ON t.id=te.task_id
                WHERE te.entity_id IN ({q}) AND t.id<>? AND {_ACTIVE}
                ORDER BY t.id""", (*ids, task_id)).fetchall()

    log_rows = conn.execute(
        "SELECT ts, actor, action, text FROM task_log WHERE task_id=? ORDER BY id",
        (task_id,)).fetchall()

    return {"task": t, "brief_num": _brief_num(conn, task_id), "evidence": ev,
            "entities": ents, "siblings": siblings, "log": log_rows}


# The brief emails name every entity in the database, so an unfiltered entity
# search returns yesterday's brief as "background reading" on every single task.
_NOT_SELF = "d.title NOT LIKE '%Chief of Staff%'"


def entity_documents(conn, task_id: int, limit: int = 12) -> list:
    """Recent source material mentioning this task's entities, the background
    reading a colleague would have done before starting. LIKE, not FTS: the
    corpus is a few hundred documents and an index would be one more thing to
    keep in sync."""
    ents = _entity_names(conn, task_id)
    if not ents:
        return []
    where = " OR ".join(["(d.title LIKE ? OR d.body LIKE ?)"] * len(ents))
    args = []
    for e in ents:
        args += [f"%{e}%", f"%{e}%"]
    return conn.execute(
        f"""SELECT d.id, d.source, d.ts, d.title, d.direction, length(d.body) AS body_len
            FROM documents d WHERE ({where}) AND {_NOT_SELF}
            ORDER BY d.ts DESC LIMIT ?""",
        (*args, limit)).fetchall()


def document(conn, doc_id: int):
    return conn.execute(
        "SELECT *, length(body) AS body_len FROM documents WHERE id=?", (doc_id,)).fetchone()


def search_documents(conn, needle: str, limit: int = 15) -> list:
    return conn.execute(
        f"""SELECT d.id, d.source, d.ts, d.title, d.direction, length(d.body) AS body_len
            FROM documents d WHERE (d.title LIKE ? OR d.body LIKE ?) AND {_NOT_SELF}
            ORDER BY d.ts DESC LIMIT ?""",
        (f"%{needle}%", f"%{needle}%", limit)).fetchall()


# ── mutations ──────────────────────────────────────────────────────────
def log(conn, task_id: int, action: str, text: str | None = None,
        actor: str = "cos_skill") -> None:
    conn.execute("INSERT INTO task_log(task_id, ts, actor, action, text) VALUES(?,?,?,?,?)",
                 (task_id, db.now(), actor, action, (text or "")[:2000]))
    conn.execute("UPDATE tasks SET updated_at=? WHERE id=?", (db.now(), task_id))
    conn.commit()


def set_status(conn, task_id: int, status: str, reason: str,
               text: str | None = None) -> None:
    closing = status in ("done", "dismissed")
    conn.execute(
        "UPDATE tasks SET status=?, closed_at=?, close_reason=?, updated_at=? WHERE id=?",
        (status, db.now() if closing else None, reason if closing else None,
         db.now(), task_id))
    log(conn, task_id, status, text)


def reopen(conn, task_id: int, text: str | None = None) -> None:
    conn.execute("""UPDATE tasks SET status='open', closed_at=NULL, close_reason=NULL,
                    snooze_until=NULL, updated_at=? WHERE id=?""", (db.now(), task_id))
    log(conn, task_id, "reopened", text)


_SETTABLE = ("due", "snooze_until", "title", "detail", "promiser", "skill", "kind")


def set_field(conn, task_id: int, field: str, value, text: str | None = None) -> None:
    if field not in _SETTABLE:
        raise ValueError(f"field {field!r} is not settable")
    conn.execute(f"UPDATE tasks SET {field}=?, updated_at=? WHERE id=?",
                 (value, db.now(), task_id))
    log(conn, task_id, field, text or str(value))


def create(conn, title: str, *, kind: str = "todo", detail: str | None = None,
           due: str | None = None, promiser: str | None = None,
           promisee: str | None = None, entity: str | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO tasks(kind, title, detail, promiser, promisee, status, due,
                             confidence, first_seen, updated_at)
           VALUES(?,?,?,?,?, 'open', ?, 1.0, ?, ?)""",
        (kind, title[:300], detail, promiser, promisee, due, db.now(), db.now()))
    tid = cur.lastrowid
    if entity:
        eid = entities.resolve(conn, entity, "company")
        if eid:
            conn.execute("INSERT OR IGNORE INTO task_entities(task_id, entity_id) VALUES(?,?)",
                         (tid, eid))
    log(conn, tid, "created", title)
    return tid
