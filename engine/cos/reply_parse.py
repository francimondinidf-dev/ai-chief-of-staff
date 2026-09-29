"""Parse your free-text reply to the brief into state changes.

You write "1 3 done, 7 chase Sophie Friday, 9 not mine, 4 is Qonto not Konto
Move" and this turns it into closes, snoozes, dismissals, entity corrections and
standing rules. Everything is logged to `corrections` first, so a
mis-interpretation is auditable and reversible rather than a silent mutation.
"""
import json
import re

from . import db, entities, llm

_QUOTED = re.compile(r"^\s*(>|On .+wrote:|From:|Sent:|-----Original)", re.I)

PROMPT = """The user replied to their daily brief. Convert the reply into operations.

Item numbers refer to the numbered list below. The user writes casually and may \
combine several instructions in one sentence.

THE BRIEF ITEMS:
{items}

THE USER'S REPLY:
\"\"\"{reply}\"\"\"

Operations you may emit:
  done         - completed. {{"op":"done","num":N}}
  not_mine     - not the user's task / not a real task. {{"op":"not_mine","num":N}}
  waiting      - flip to waiting-on someone. {{"op":"waiting","num":N,"who":"..."}}
  snooze       - resurface later. {{"op":"snooze","num":N,"until":"YYYY-MM-DD"}}
  due          - set/change a deadline. {{"op":"due","num":N,"date":"YYYY-MM-DD"}}
  retitle      - fix the wording. {{"op":"retitle","num":N,"title":"..."}}
  alias        - a name correction. {{"op":"alias","from":"Konto","to":"Qonto"}}
  rule         - a standing instruction to remember.
                 {{"op":"rule","kind":"disambiguation|never_task|preference","text":"..."}}
  note         - anything else worth recording. {{"op":"note","num":N,"text":"..."}}

If the user states a general principle ("always assume portfolio company when \
ambiguous", "Sophie's requests are never tasks for me"), emit a `rule` IN ADDITION \
to any per-item operation, so it applies in future.

Today is {today}. Resolve relative dates ("Friday", "next week") against it.

Reply with JSON ONLY: {{"ops": [ ... ]}}"""


def strip_quoted(body: str) -> str:
    """Keep only what the user actually typed, above the quoted brief."""
    lines = []
    for line in (body or "").splitlines():
        if _QUOTED.match(line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _brief_items(conn, brief_id: int):
    return conn.execute(
        """SELECT bi.num, t.id, t.title, t.kind, t.status FROM brief_items bi
           JOIN tasks t ON t.id = bi.task_id
           WHERE bi.brief_id = ? ORDER BY bi.num""", (brief_id,)
    ).fetchall()


def apply_reply(conn, reply_text: str, brief_id: int | None = None,
                channel: str = "email_reply") -> dict:
    from datetime import date

    if brief_id is None:
        row = conn.execute("SELECT id FROM briefs ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return {"applied": 0, "error": "no brief to reply to"}
        brief_id = row["id"]

    items = _brief_items(conn, brief_id)
    if not items:
        return {"applied": 0, "error": "brief had no items"}

    cur = conn.execute(
        "INSERT INTO corrections(ts, channel, raw) VALUES(?,?,?)",
        (db.now(), channel, reply_text[:8000]),
    )
    correction_id = cur.lastrowid
    conn.commit()

    listing = "\n".join(f'{r["num"]}. [{r["kind"]}/{r["status"]}] {r["title"]}' for r in items)
    try:
        parsed = llm.ask_json(PROMPT.format(
            items=listing, reply=reply_text[:6000], today=date.today().isoformat()))
    except Exception as exc:
        conn.execute("UPDATE corrections SET parsed=? WHERE id=?",
                     (json.dumps({"error": repr(exc)}), correction_id))
        conn.commit()
        return {"applied": 0, "error": "could not parse reply"}

    by_num = {r["num"]: r["id"] for r in items}
    ops = parsed.get("ops", []) or []
    applied = 0

    for op in ops:
        kind = op.get("op")
        tid = by_num.get(op.get("num")) if op.get("num") is not None else None
        try:
            if kind == "done" and tid:
                conn.execute("""UPDATE tasks SET status='done', closed_at=?, updated_at=?,
                                close_reason='user_confirmed' WHERE id=?""",
                             (db.now(), db.now(), tid))
            elif kind == "not_mine" and tid:
                conn.execute("""UPDATE tasks SET status='dismissed', closed_at=?, updated_at=?,
                                close_reason='dismissed' WHERE id=?""",
                             (db.now(), db.now(), tid))
            elif kind == "waiting" and tid:
                conn.execute("""UPDATE tasks SET kind='waiting_on', promiser=COALESCE(?,promiser),
                                status='open', updated_at=? WHERE id=?""",
                             (op.get("who"), db.now(), tid))
            elif kind == "snooze" and tid:
                conn.execute("UPDATE tasks SET snooze_until=?, updated_at=? WHERE id=?",
                             (op.get("until"), db.now(), tid))
            elif kind == "due" and tid:
                conn.execute("UPDATE tasks SET due=?, updated_at=? WHERE id=?",
                             (op.get("date"), db.now(), tid))
            elif kind == "retitle" and tid and op.get("title"):
                conn.execute("UPDATE tasks SET title=?, updated_at=? WHERE id=?",
                             (op["title"][:300], db.now(), tid))
            elif kind == "alias" and op.get("from") and op.get("to"):
                eid = entities.resolve(conn, op["to"], "company")
                if eid:
                    entities.add_alias(conn, op["from"], eid, "correction")
            elif kind == "rule" and op.get("text"):
                conn.execute(
                    """INSERT INTO rules(kind, text, created_at, source_correction_id)
                       VALUES(?,?,?,?)""",
                    (op.get("kind", "preference"), op["text"][:500], db.now(), correction_id))
            elif kind == "note" and tid:
                conn.execute("UPDATE tasks SET detail=COALESCE(detail||' | ','')||?, "
                             "updated_at=? WHERE id=?",
                             ((op.get("text") or "")[:300], db.now(), tid))
            else:
                continue
            applied += 1
        except Exception:
            continue

    conn.execute("UPDATE corrections SET parsed=?, applied=1 WHERE id=?",
                 (json.dumps(ops, ensure_ascii=False), correction_id))
    conn.commit()
    return {"applied": applied, "ops": ops}
