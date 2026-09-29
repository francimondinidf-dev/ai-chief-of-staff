"""Recursive Notion block -> plain text.

Notion AI meeting notes live in `transcription` blocks whose text is NOT in the
block's own rich_text, it sits several levels down in nested children. A
non-recursive read returns an empty string and silently looks like "no
transcript", so everything here walks the full tree.
"""
import time
import urllib.error
import urllib.request
import json

from . import config

API = "https://api.notion.com/v1"
VERSION = "2022-06-28"

# Block types whose rich_text we want. Everything else we still recurse into.
_TEXT_KEYS = ("rich_text", "title", "caption")


def _req(path: str, method: str = "GET", body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{API}{path}", data=data, method=method,
        headers={
            "Authorization": f"Bearer {config.NOTION_API_KEY}",
            "Notion-Version": VERSION,
            "Content-Type": "application/json",
        },
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 429:                       # documented rate limit
                time.sleep(1.5 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"notion: gave up on {path}")


def _block_text(block: dict) -> str:
    payload = block.get(block.get("type"), {}) or {}
    out = []
    for key in _TEXT_KEYS:
        for span in payload.get(key) or []:
            out.append(span.get("plain_text", ""))
    return "".join(out)


def page_text(page_id: str, max_depth: int = 8, _depth: int = 0) -> str:
    """Full plain text of a page or block subtree, in document order."""
    if _depth > max_depth:
        return ""
    lines, cursor = [], None
    while True:
        q = f"?page_size=100" + (f"&start_cursor={cursor}" if cursor else "")
        resp = _req(f"/blocks/{page_id}/children{q}")
        for block in resp.get("results", []):
            text = _block_text(block).strip()
            if text:
                lines.append(text)
            if block.get("has_children"):
                nested = page_text(block["id"], max_depth, _depth + 1)
                if nested:
                    lines.append(nested)
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return "\n".join(lines)
