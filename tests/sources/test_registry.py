from pathlib import Path

from pipeline.sources.registry import SOURCES, TOKENLESS_SOURCES, load_targets

EXAMPLE = Path(__file__).resolve().parents[2] / "profile.example" / "targets.yaml"


def test_every_registered_source_answers_to_its_own_key():
    # A mismatch here silently disables a source: the orchestrator looks it up
    # by key, the adapter reads cfg.targets by self.name, and nothing errors.
    for key, source in SOURCES.items():
        assert source.name == key


def test_load_targets_reads_the_example_file():
    targets = load_targets(EXAMPLE)
    assert "stripe" in targets["greenhouse"]


def test_load_targets_returns_empty_for_a_missing_file(tmp_path):
    # profile/ is gitignored and absent on a fresh clone; that must not crash.
    assert load_targets(tmp_path / "nope.yaml") == {}


def test_load_targets_tolerates_an_empty_section(tmp_path):
    path = tmp_path / "t.yaml"
    path.write_text("greenhouse:\nlever:\n  - netflix\n")
    assert load_targets(path) == {"greenhouse": [], "lever": ["netflix"]}


def test_example_targets_cover_every_token_based_source():
    targets = load_targets(EXAMPLE)
    for key in SOURCES.keys() - TOKENLESS_SOURCES:
        assert targets.get(key), f"{key} has no example tokens"


def test_aggregators_need_no_tokens():
    # Remotive and HN cover the whole market rather than a company list. Without
    # this set, the check above would demand tokens that cannot exist.
    assert TOKENLESS_SOURCES <= SOURCES.keys()
    targets = load_targets(EXAMPLE)
    for key in TOKENLESS_SOURCES:
        assert not targets.get(key), f"{key} should not need tokens"
