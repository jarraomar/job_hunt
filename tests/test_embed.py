import math

import pytest

from pipeline.embed import DIMENSIONS, Embedder, cosine, get_embedder


@pytest.fixture(scope="module")
def embedder():
    return Embedder()


def test_embeds_to_the_expected_dimension(embedder):
    vec = embedder.embed_one("Senior Software Engineer, Python and React")
    assert len(vec) == DIMENSIONS


def test_embedding_is_deterministic(embedder):
    a = embedder.embed_one("Backend Engineer")
    b = embedder.embed_one("Backend Engineer")
    assert a == b


def test_related_text_scores_higher_than_unrelated(embedder):
    """The property the whole cheap gate rests on.

    If this does not hold, the embedding gate is not separating anything and
    every downstream cost estimate is wrong.
    """
    resume = "Full-stack engineer. Python, FastAPI, React, AWS, Docker, PostgreSQL."
    close = embedder.embed_one("Senior Full Stack Engineer — Python, React, AWS")
    far = embedder.embed_one("Registered Nurse, ICU night shift, BLS certification")
    base = embedder.embed_one(resume)
    assert cosine(base, close) > cosine(base, far)


def test_cosine_of_identical_vectors_is_one(embedder):
    vec = embedder.embed_one("anything")
    assert cosine(vec, vec) == pytest.approx(1.0, abs=1e-6)


def test_cosine_handles_a_zero_vector_without_dividing_by_zero():
    # An empty description would otherwise raise mid-run and lose the batch.
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_cosine_is_bounded():
    a = [1.0, 0.0]
    b = [-1.0, 0.0]
    assert -1.0 <= cosine(a, b) <= 1.0
    assert cosine(a, b) == pytest.approx(-1.0)


def test_batch_embedding_matches_single(embedder):
    texts = ["Backend Engineer", "Frontend Engineer"]
    batch = embedder.embed(texts)
    assert len(batch) == 2
    assert batch[0] == embedder.embed_one(texts[0])


def test_empty_text_does_not_raise(embedder):
    vec = embedder.embed_one("")
    assert len(vec) == DIMENSIONS
    assert not any(math.isnan(x) for x in vec)


def test_get_embedder_returns_a_singleton():
    # A serverless instance serves many invocations; re-loading the model per
    # request would pay the load cost every time instead of once.
    assert get_embedder() is get_embedder()
