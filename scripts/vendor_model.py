"""Download the embedding model into the build.

Runs as Vercel's build command. The alternative -- letting fastembed fetch the
model at runtime -- would download 65 MB into an ephemeral /tmp on every cold
start, which is both slow and repeated.

Idempotent: fastembed skips the download when the cache already holds the model,
so this is cheap on a warm local run and does the real work on a clean builder.
"""

from __future__ import annotations

import sys
from pathlib import Path

from pipeline.embed import MODEL_NAME

CACHE = Path(__file__).resolve().parents[1] / "model_cache"

# The Python function bundle limit. Measured locally the whole bundle lands near
# 292 MB, so this is a wide margin -- but it is checked rather than assumed,
# because exceeding it fails at deploy with no obvious cause.
BUNDLE_LIMIT_MB = 500


def main() -> int:
    CACHE.mkdir(parents=True, exist_ok=True)

    from fastembed import TextEmbedding

    TextEmbedding(MODEL_NAME, cache_dir=str(CACHE))

    size_mb = sum(f.stat().st_size for f in CACHE.rglob("*") if f.is_file()) / 1e6
    print(f"vendored {MODEL_NAME} into {CACHE} ({size_mb:.0f} MB)")

    if size_mb > BUNDLE_LIMIT_MB:
        print(f"model alone exceeds the {BUNDLE_LIMIT_MB} MB function limit", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
