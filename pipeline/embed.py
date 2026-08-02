"""Local embeddings. Gate 2 of the scoring pipeline (spec section 8).

This is what makes the design affordable: it costs nothing, needs no API key,
and cuts ~800 daily jobs down to the few dozen worth paying a model to judge.
Judging everything instead would run about $4/day.

Measured on the dev machine: 0.7s cold-start cost, 65 MB model. The ONNX model
is memory-mapped rather than parsed, so the load itself is 0.16s -- almost all
of the cold start is importing the library.

Throughput depends entirely on document length, and the headline number from a
synthetic benchmark does not transfer:

    short strings (~50 chars)   449 docs/sec
    real job descriptions        11 docs/sec   <- the one that matters

Scoring 813 live postings takes ~74s, not the two seconds the synthetic figure
implies. That still fits the 600s run budget with room, but it is the dominant
cost of a scoring pass and it scales linearly -- budget for it if the corpus
grows past a few thousand live jobs.
"""

from __future__ import annotations

import logging
import math
import os
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384

# Vendored into the bundle at build time (scripts/vendor_model.py) so a cold
# start never downloads 65 MB into an ephemeral /tmp.
_DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "model_cache"

_embedder: Embedder | None = None


class Embedder:
    """Thin wrapper over fastembed. Constructed once per process."""

    def __init__(self, cache_dir: Path | None = None) -> None:
        # Imported lazily: the web routes that never score should not pay the
        # import cost, and tests that do not embed should not need the model.
        from fastembed import TextEmbedding

        resolved = cache_dir or Path(os.environ.get("JOBHUNT_MODEL_CACHE", _DEFAULT_CACHE))
        self._model = TextEmbedding(MODEL_NAME, cache_dir=str(resolved))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def get_embedder() -> Embedder:
    """The process-wide embedder, created on first use."""
    global _embedder
    if _embedder is None:
        _embedder = Embedder()
    return _embedder


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, returning 0.0 rather than raising on a zero vector.

    An empty job description embeds to something near zero; raising here would
    lose the whole batch for one bad row.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)
