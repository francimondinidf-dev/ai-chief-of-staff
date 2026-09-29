"""Paths, credentials, tunables. No secrets live in this file."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
DB_PATH = DATA / "cos.db"

# Everything personal (who you are, your Notion IDs, keys) lives in .env next to
# run.py. Copy .env.example to .env and fill it in; nothing is hard-coded here.
_EXTRA_ENVS = [
    ROOT / ".env",
]


def _load_env() -> None:
    for path in _EXTRA_ENVS:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            # first file wins, so the project's own .env overrides the borrowed ones
            os.environ.setdefault(key, val)


_load_env()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("COS_NOTION_API_KEY") or os.environ.get("NOTION_API_KEY", "")
# Call-transcript sources. The team "Meeting Notes" database is shared, so it is
# filtered to calls you created or attended. A private calls database (optional)
# is read in full. Leave either blank to skip it.
NOTION_TEAM_CALLS_DB = os.environ.get("COS_NOTION_TEAM_CALLS_DB", "")
NOTION_PRIVATE_CALLS_DB = os.environ.get("COS_NOTION_PRIVATE_CALLS_DB", "")
# Your Notion user id: how "calls you attended" is recognised in the team database.
# If blank, attendance is matched on your email instead.
NOTION_USER_ID = os.environ.get("COS_NOTION_USER_ID", "")

USER_EMAIL = os.environ.get("COS_USER_EMAIL", "").strip()
USER_NAME = os.environ.get("COS_USER_NAME", "").strip()
USER_ROLE = os.environ.get("COS_USER_ROLE", "an investor at a growth-equity fund").strip()
NOTION_PEOPLE_PROPS = tuple(p.strip() for p in os.environ.get(
    "COS_NOTION_PEOPLE_PROPS", "Attendees,Participants").split(",") if p.strip())
if not USER_EMAIL or not USER_NAME:
    raise SystemExit("Chief of Staff: set COS_USER_EMAIL and COS_USER_NAME in "
                     f"{ROOT / '.env'} (copy .env.example to start).")

# Models. Re-verify against the live Models API every couple of months.
# Sonnet 5 for extraction: the hard part is judging "real commitment vs polite
# filler", and that judgment is what decides whether the brief stays trusted.
MODEL_EXTRACT = os.environ.get("COS_MODEL_EXTRACT", "claude-sonnet-5")
MODEL_REASON = os.environ.get("COS_MODEL_REASON", "claude-sonnet-5")

# Which credential the tool presents when it talks to Claude:
#   "cc"  -> Claude Code headless (`claude -p`), draws on your subscription
#   "api" -> ANTHROPIC_API_KEY, metered pay-per-token (automatic fallback)
BACKEND = os.environ.get("COS_BACKEND", "cc")
# Documents per headless invocation. Claude Code carries ~28k tokens of its own
# system prompt per call, so batching amortizes it. Nothing user-visible.
BATCH_SIZE = int(os.environ.get("COS_BATCH_SIZE", "10"))

# Ingest window on a cold start; afterwards the watermark takes over.
COLD_START_DAYS = int(os.environ.get("COS_COLD_START_DAYS", "14"))
# A commitment with no movement for this long stops being "to do" and becomes a
# question: is this still real? Left in the main list it quietly rots and trains
# you to skim past the section.
STALE_AFTER_DAYS = int(os.environ.get("COS_STALE_AFTER_DAYS", "21"))
# Group a person's "waiting on" items once they owe you at least this many, so
# one chatty call can't flood the section with five separate lines.
GROUP_WAITING_AT = int(os.environ.get("COS_GROUP_WAITING_AT", "3"))
# Ignore mail bodies longer than this (newsletters, huge threads) after truncation.
MAX_BODY_CHARS = 20_000
# Transcripts get far more room, and are never head-truncated: the action items
# on a call live in the last few minutes ("so, next steps..."), so cutting the
# tail is exactly the wrong end to lose. See trim_transcript().
MAX_TRANSCRIPT_CHARS = 80_000
