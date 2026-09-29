"""Notion call-transcript ingest.

Reads from one or more call databases (set in .env, see config.py):

  * the shared team "Meeting Notes"      -> filtered to calls you created or
                                            attended, so the brief isn't full
                                            of your colleagues' commitments
  * an optional private calls database   -> every row is yours, no filter

Both share the same schema (the private one is a clone of the team template),
so a single row parser handles them. Pages are re-read when Notion's
last_edited_time moves, because the transcript lands after the page is created.
"""
import json
from datetime import datetime, timedelta, timezone

from . import config, db, notion_blocks as nb

# (database_id, mine_only), mine_only applies the attendee filter.
CALL_DATABASES = [(db_id, mine_only) for db_id, mine_only in (
    (config.NOTION_PRIVATE_CALLS_DB, False),   # your own calls database, if any
    (config.NOTION_TEAM_CALLS_DB, True),       # team Meeting Notes
) if db_id]

USER_NOTION_ID = config.NOTION_USER_ID
USER_EMAIL = config.USER_EMAIL.lower()

_PEOPLE_PROPS = config.NOTION_PEOPLE_PROPS
_TITLE_FALLBACK = "(untitled call)"


def _title(props: dict) -> str:
    for p in props.values():
        if p.get("type") == "title":
            spans = p.get("title") or []
            return "".join(s.get("plain_text", "") for s in spans).strip() or _TITLE_FALLBACK
    return _TITLE_FALLBACK


def _people(props: dict) -> list[dict]:
    out, seen = [], set()
    for key in _PEOPLE_PROPS:
        prop = props.get(key)
        if not prop or prop.get("type") != "people":
            continue
        for person in prop.get("people") or []:
            pid = person.get("id")
            if pid in seen:
                continue
            seen.add(pid)
            out.append({
                "name": person.get("name"),
                "email": ((person.get("person") or {}).get("email") or "").lower(),
                "id": pid,
                "role": "attendee",
            })
    return out


def _when(page: dict) -> str:
    props = page.get("properties", {})
    for key in ("Date", "Adjusting Date"):
        prop = props.get(key)
        if prop and prop.get("type") == "date" and (prop.get("date") or {}).get("start"):
            return prop["date"]["start"]
    return page.get("created_time", "")


def _is_mine(page: dict) -> bool:
    if USER_NOTION_ID and (page.get("created_by") or {}).get("id") == USER_NOTION_ID:
        return True
    return any((USER_NOTION_ID and p.get("id") == USER_NOTION_ID) or p.get("email") == USER_EMAIL
               for p in _people(page.get("properties", {})))


def trim_transcript(text: str, limit: int | None = None) -> str:
    """Keep the opening and, crucially, the close.

    Commitments on a call are made in the last few minutes. A plain head-
    truncation keeps the greetings and drops every action item, which is the
    one failure mode that would make the brief systematically useless.
    """
    limit = limit or config.MAX_TRANSCRIPT_CHARS
    if len(text) <= limit:
        return text
    head = int(limit * 0.3)
    tail = limit - head
    return (text[:head]
            + "\n\n[... middle of transcript trimmed ...]\n\n"
            + text[-tail:])


def _call_type(props: dict) -> str | None:
    tag = props.get("Call Type Tag")
    if tag and tag.get("type") == "select" and tag.get("select"):
        return tag["select"].get("name")
    return None


def ingest(conn, cold_start_days: int | None = None) -> dict:
    stats = {"new": 0, "seen": 0, "skipped_others": 0, "empty": 0, "errors": []}
    days = cold_start_days or config.COLD_START_DAYS
    mark = db.get_watermark(conn, "notion_calls")
    since = (datetime.fromisoformat(mark) - timedelta(hours=6)) if mark else (
        datetime.now(timezone.utc) - timedelta(days=days))
    newest = None

    for database_id, mine_only in CALL_DATABASES:
        cursor = None
        while True:
            body = {
                "page_size": 100,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                "filter": {"timestamp": "last_edited_time", "last_edited_time":
                           {"on_or_after": since.date().isoformat()}},
            }
            if cursor:
                body["start_cursor"] = cursor
            try:
                resp = nb._req(f"/databases/{database_id}/query", "POST", body)
            except Exception as exc:
                stats["errors"].append(f"{database_id[:8]}: {exc!r}"[:200])
                break

            for page in resp.get("results", []):
                try:
                    if mine_only and not _is_mine(page):
                        stats["skipped_others"] += 1
                        continue
                    props = page.get("properties", {})
                    text = nb.page_text(page["id"])
                    if len(text.strip()) < 80:      # meeting created, transcript not in yet
                        stats["empty"] += 1
                        continue
                    edited = page.get("last_edited_time", "")
                    if newest is None or edited > newest:
                        newest = edited
                    _, is_new = db.upsert_document(
                        conn,
                        source="notion_call",
                        source_id=page["id"],
                        ts=_when(page),
                        title=_title(props),
                        participants=_people(props),
                        direction="meeting",
                        body=trim_transcript(text),
                        meta={"url": page.get("url"),
                              "database_id": database_id,
                              "call_type": _call_type(props)},
                        version=edited,
                    )
                    stats["new" if is_new else "seen"] += 1
                except Exception as exc:
                    stats["errors"].append(f"page {page.get('id','?')[:8]}: {exc!r}"[:200])
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
        conn.commit()

    if newest:
        db.set_watermark(conn, "notion_calls", newest)
        conn.commit()
    return stats
