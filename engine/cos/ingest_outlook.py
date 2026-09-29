"""Outlook ingest via COM.

Two hard-won lessons from earlier Outlook automations are baked in:
  * Items.Restrict() with a date filter silently returns 0 under some locales,
    so we iterate and filter in Python instead.
  * Items.Sort() silently fails under Task Scheduler, so we never `break` out of
    the loop on an old item, we `continue`, or we lose everything behind it.
"""
import re
from datetime import datetime, timedelta, timezone

import pythoncom
import win32com.client

from . import config, db

OL_INBOX, OL_SENT = 6, 5
OL_MAIL_ITEM = 43

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t]*\n\s*\n\s*", re.M)

# "This is you replying to your own brief", the reply prefix is load-bearing.
# The outgoing brief also lands in Sent with the same subject, so matching on the
# subject alone makes the tool read its own email as an instruction.
BRIEF_SUBJECT = "Chief of Staff"
_REPLY_PREFIX = re.compile(r"^\s*(re|r|rép|rep|tr|fw|fwd)\s*[:：]", re.I)


def _is_brief_reply(subject: str) -> bool:
    return bool(_REPLY_PREFIX.match(subject or "")) and BRIEF_SUBJECT.lower() in (subject or "").lower()


def _plain(item) -> str:
    """Prefer Outlook's own plaintext rendering; fall back to stripping HTML."""
    body = ""
    try:
        body = item.Body or ""
    except Exception:
        pass
    if not body.strip():
        try:
            body = _TAG.sub(" ", item.HTMLBody or "")
        except Exception:
            body = ""
    body = _WS.sub("\n\n", body).strip()
    return body[: config.MAX_BODY_CHARS]


def _to_utc(value) -> datetime | None:
    """pywintypes datetimes are datetime subclasses but carry odd tzinfo."""
    if value is None:
        return None
    try:
        dt = datetime(value.year, value.month, value.day,
                      value.hour, value.minute, value.second)
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _is_self_note(people: list[dict], subject: str) -> bool:
    """An email you sent only to yourself, how tasks from Teams or corridor
    conversations reach the system. Treated as an explicit task declaration, not
    as conversational material to be second-guessed.

    Excludes replies to the brief, which are also self-addressed but are
    instructions about existing items rather than new ones.
    """
    if _is_brief_reply(subject):
        return False
    me = (config.USER_EMAIL or "").lower()
    senders = {p["email"] for p in people if p.get("role") == "from" and p.get("email")}
    to = {p["email"] for p in people if p.get("role") == "to" and p.get("email")}
    return bool(to) and to <= {me} and (not senders or senders <= {me})


def _people(item) -> list[dict]:
    out = []
    try:
        sender = (item.SenderEmailAddress or "").strip()
        if sender:
            out.append({"name": (item.SenderName or "").strip(),
                        "email": sender.lower(), "role": "from"})
    except Exception:
        pass
    try:
        for rcp in item.Recipients:
            try:
                out.append({"name": (rcp.Name or "").strip(),
                            "email": (rcp.Address or "").strip().lower(),
                            "role": "to"})
            except Exception:
                continue
    except Exception:
        pass
    return out


def _walk(folder, source: str, since: datetime, conn, stats: dict) -> str | None:
    items = folder.Items
    try:
        items.Sort("[ReceivedTime]", True)   # best effort; may silently no-op
    except Exception:
        pass

    newest = None
    for item in items:
        try:
            if getattr(item, "Class", None) != OL_MAIL_ITEM:
                continue
            ts = _to_utc(getattr(item, "ReceivedTime", None) or getattr(item, "SentOn", None))
            if ts is None:
                continue
            if ts < since:
                continue                     # NOT break, Sort() may have no-opped
            if newest is None or ts > newest:
                newest = ts

            subject = (item.Subject or "").strip()
            people = _people(item)
            self_note = _is_self_note(people, subject)
            doc_id, is_new = db.upsert_document(
                conn,
                source=source,
                source_id=item.EntryID,
                ts=ts.isoformat(timespec="seconds"),
                title=subject,
                participants=people,
                direction="self" if self_note else
                          ("out" if source == "outlook_sent" else "in"),
                body=_plain(item),
                meta={
                    "conversation_id": getattr(item, "ConversationID", None),
                    "folder": folder.Name,
                    "is_brief_reply": source == "outlook_sent" and _is_brief_reply(subject),
                    "self_note": self_note,
                },
            )
            stats["new" if is_new else "seen"] += 1
        except Exception as exc:            # one bad item must not kill the run
            stats["errors"].append(f"{source}: {exc!r}"[:200])
            continue
    return newest.isoformat(timespec="seconds") if newest else None


def ingest(conn, cold_start_days: int | None = None) -> dict:
    pythoncom.CoInitialize()
    stats = {"new": 0, "seen": 0, "errors": []}
    try:
        ns = win32com.client.Dispatch("Outlook.Application").GetNamespace("MAPI")
        for source, const in (("outlook_inbox", OL_INBOX), ("outlook_sent", OL_SENT)):
            mark = db.get_watermark(conn, source)
            if mark:
                since = datetime.fromisoformat(mark) - timedelta(hours=1)  # overlap
            else:
                days = cold_start_days or config.COLD_START_DAYS
                since = datetime.now(timezone.utc) - timedelta(days=days)
            newest = _walk(ns.GetDefaultFolder(const), source, since, conn, stats)
            if newest:
                db.set_watermark(conn, source, newest)
            conn.commit()
    finally:
        pythoncom.CoUninitialize()
    return stats
