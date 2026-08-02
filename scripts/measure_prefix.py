"""Measure the judge's static prefix against the model's cache minimum.

The number this prints decides whether prompt caching does anything at all.
Below the minimum, `cache_control` is accepted and silently ignored:
cache_creation_input_tokens comes back 0, with no error and no warning
(spec section 11).

Counter-intuitively, the fix for a short prefix is to make it LONGER. Above the
threshold the whole block bills at 0.1x instead of 1x:

    prefix below the minimum (caching no-ops)  ~$0.0054 / judgement
    prefix grown past the minimum              ~$0.0029 / judgement

Costs one count_tokens call, which is free.
"""

from __future__ import annotations

import anthropic

from pipeline.config import load_settings
from pipeline.judge import MODEL, build_static_prefix
from pipeline.llm import MIN_CACHEABLE_TOKENS
from pipeline.profile import load_profile


def main() -> int:
    settings = load_settings()
    if not settings.anthropic_api_key:
        print("ANTHROPIC_API_KEY is not set")
        return 1

    profile = load_profile(settings)
    prefix = build_static_prefix(profile)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    counted = client.messages.count_tokens(
        model=MODEL,
        system=[{"type": "text", "text": prefix}],
        messages=[{"role": "user", "content": "x"}],
    ).input_tokens

    minimum = MIN_CACHEABLE_TOKENS[MODEL]
    print(f"model:            {MODEL}")
    print(f"prefix chars:     {len(prefix):,}")
    print(f"prefix tokens:    {counted:,}")
    print(f"cache minimum:    {minimum:,}")

    if counted >= minimum:
        print(f"\nVERDICT: caching WORKS ({counted - minimum:,} tokens of headroom)")
        return 0

    print(f"\nVERDICT: caching NO-OPS — {minimum - counted:,} tokens short")
    print("Grow the prefix with genuinely useful content (fuller résumé detail,")
    print("more competency bullets, worked scoring examples) rather than dropping")
    print("caching: above the threshold the whole block bills at 0.1x.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
