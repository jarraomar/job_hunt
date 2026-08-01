"""Text cleanup shared by the source adapters."""

from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(raw: str | None) -> str:
    """Turn a description fragment into plain text.

    Unescapes twice on purpose: Greenhouse serves entity-escaped markup, so the
    first pass produces tags and the second catches entities that were
    themselves escaped ("&amp;lt;").
    """
    text = html.unescape(raw or "")
    text = _TAG_RE.sub(" ", text)
    return _WS_RE.sub(" ", html.unescape(text)).strip()


def join_sections(*parts: str | None) -> str:
    """Concatenate description fragments, dropping empties.

    Sources split a posting across several fields and compensation frequently
    lives in one of the trailing ones, so parsing only the main body loses it.
    """
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
