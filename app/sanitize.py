"""Input sanitization for data flowing from external sources (Linear) into
prompts, file paths, and git operations.

Defence-in-depth: even though Claude Code runs in a container with
--dangerously-skip-permissions, we still limit what untrusted data can do.
"""
from __future__ import annotations

import re


# ── Identifier validation ───────────────────────────────────────────
# Linear identifiers follow the pattern TEAM-123 (letters + dash + digits).
_IDENTIFIER_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")


def validate_identifier(identifier: str) -> str:
    """Ensure an identifier looks like a Linear ticket (e.g. PROJ-42).

    Prevents path traversal and command injection via crafted identifiers
    used in branch names (ticket/{identifier}) and file paths.
    Raises ValueError if invalid.
    """
    identifier = identifier.strip()
    if not _IDENTIFIER_RE.match(identifier):
        raise ValueError(f"Invalid identifier format: {identifier!r}")
    return identifier


def safe_identifier(identifier: str) -> str:
    """Like validate_identifier but returns a sanitized fallback instead
    of raising.  Strips everything except [A-Za-z0-9-]."""
    cleaned = re.sub(r"[^A-Za-z0-9\-]", "", identifier.strip())
    return cleaned or "UNKNOWN-0"


# ── Prompt content fencing ──────────────────────────────────────────

def fence_user_content(text: str, label: str = "user-provided content") -> str:
    """Wrap untrusted text in XML-style fence tags so the LLM can
    distinguish system instructions from user-supplied data.

    This is not a bulletproof defence against prompt injection, but it
    significantly reduces the chance of accidental instruction override
    and makes intentional attacks more obvious in logs.
    """
    if not text:
        return ""
    # Escape any closing tags inside the content to prevent breakout
    safe_text = text.replace("</user-content>", "&lt;/user-content&gt;")
    return f"<user-content source=\"{label}\">\n{safe_text}\n</user-content>"


def sanitize_for_prompt(text: str, max_length: int = 50_000) -> str:
    """Basic sanitisation of free-text fields before prompt inclusion.

    - Truncates to max_length to prevent context-window abuse
    - Strips null bytes
    - Normalises excessive whitespace
    """
    if not text:
        return ""
    text = text.replace("\x00", "")
    # Collapse runs of more than 3 blank lines
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    if len(text) > max_length:
        text = text[:max_length] + "\n\n[...truncated]"
    return text
