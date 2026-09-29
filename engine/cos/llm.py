"""LLM backend. Two credentials, one interface.

  cc  -> Claude Code headless (`claude -p`). Draws on the Claude subscription.
         Carries ~28k tokens of Claude Code's own system prompt per invocation,
         so callers batch. Warm cache makes repeat calls much cheaper.
  api -> ANTHROPIC_API_KEY, metered. Used as automatic fallback when `cc` fails
         (expired OAuth, CLI missing), so a scheduled run still produces a brief.

Both return parsed JSON. Models are not trusted to emit clean JSON first try,
so `_first_json` tolerates prose/fences around it and one retry is allowed.
"""
import json
import os
import re
import shutil
import subprocess

from . import config

CLAUDE_BIN = shutil.which("claude") or r"C:\Program Files (x86)\Nodist\bin\claude.cmd"
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    pass


def _first_json(text: str) -> dict | list:
    """Pull the first JSON value out of a response that may carry prose."""
    text = (text or "").strip()
    fence = _FENCE.search(text)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    # fall back to brace/bracket matching, ignoring braces inside strings
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        if start < 0:
            continue
        depth, in_str, esc = 0, False, False
        for i, ch in enumerate(text[start:], start):
            if esc:
                esc = False
                continue
            if ch == "\\" and in_str:
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == opener:
                    depth += 1
                elif ch == closer:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start:i + 1])
                        except Exception:
                            break
    raise LLMError(f"no JSON in response: {text[:300]!r}")


def _cc_env() -> dict:
    """The subprocess environment for `claude -p`.

    config._load_env() puts ANTHROPIC_API_KEY into os.environ so the `api`
    backend can use it. The CLI inherits it and refuses to start: the key takes
    precedence over the claude.ai login, so it exits 1 with "connectors are
    disabled because ANTHROPIC_API_KEY ... is set". Strip it here - `cc` must
    always mean the subscription, never the metered key.
    """
    env = dict(os.environ)
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(var, None)
    return env


def _cc_reason(proc) -> str:
    """The CLI's real complaint. It reports refusals to start -- "Not logged in
    - Please run /login", "You've hit your session limit" -- on STDOUT (inside
    the JSON envelope when there is one), and leaves STDERR empty. Reading only
    stderr is why four days of dead runs logged a bare "exit 1:" and nobody
    could tell an expired login from an exhausted quota. Always read both.
    """
    out = (proc.stdout or "").strip()
    try:
        envelope = json.loads(out)
    except Exception:
        pass
    else:
        if isinstance(envelope, dict):
            out = str(envelope.get("result") or envelope.get("error") or out)
    err = (proc.stderr or "").strip()
    return " | ".join(part[:300] for part in (out, err) if part) or "(no output)"


def _via_claude_code(prompt: str, model: str, timeout: int) -> str:
    proc = subprocess.run(
        [CLAUDE_BIN, "-p", "--output-format", "json", "--model", model,
         "--disallowed-tools", "Bash", "Edit", "Write", "WebSearch", "WebFetch"],
        input=prompt, capture_output=True, text=True, encoding="utf-8",
        timeout=timeout, shell=False, env=_cc_env(),
    )
    if proc.returncode != 0:
        raise LLMError(f"claude -p exit {proc.returncode}: {_cc_reason(proc)}")
    try:
        envelope = json.loads(proc.stdout)
    except Exception:
        # exit 0 but unparseable stdout - say what actually came back
        raise LLMError(f"claude -p gave no JSON envelope: {_cc_reason(proc)}")
    if envelope.get("is_error"):
        raise LLMError(f"claude -p error: {str(envelope.get('result'))[:300]}")
    return envelope.get("result") or ""


def _via_api(prompt: str, model: str, timeout: int) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, timeout=timeout)
    resp = client.messages.create(
        model=model, max_tokens=16000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


def ask_json(prompt: str, *, model: str | None = None, timeout: int = 600,
             retries: int = 1, backend: str | None = None) -> dict | list:
    """Send a prompt, get parsed JSON back. Falls back cc -> api on failure."""
    model = model or config.MODEL_EXTRACT
    order = [backend or config.BACKEND]
    if order[0] == "cc":
        order.append("api")               # keep the brief alive if OAuth lapses

    # Report EVERY backend's failure, not just the last. Reporting only the
    # last hid a broken `cc` behind the api fallback's billing error for weeks.
    failures = {}
    for which in order:
        runner = _via_claude_code if which == "cc" else _via_api
        for attempt in range(retries + 1):
            try:
                raw = runner(prompt, model, timeout)
                return _first_json(raw)
            except Exception as exc:
                failures[which] = exc
                if attempt < retries:
                    prompt += ("\n\nYour previous reply was not valid JSON. "
                               "Reply with the JSON value ONLY, no prose, no fences.")
    detail = "; ".join(f"{k}: {str(v)[:200]}" for k, v in failures.items())
    raise LLMError(f"all backends failed - {detail}")
