"""Offline checks for the experiment design, never call an LLM."""
from types import SimpleNamespace

from benchmark_memory import NoCompaction
from memory_tasks import TASKS, acceptance, build_workspace, fixture_text
from stellarcode.llm.types import estimate_request_tokens


def test_frozen_tasks_and_load_within_declared_window(tmp_path):
    for name in TASKS:
        prompts = build_workspace(tmp_path / name, name)
        assert len(prompts) == 12
        assert len(set(prompts)) == 12
        corpus = [fixture_text(name, turn) for turn in range(1, 13)]
        assert len(set(corpus)) == 12
        assert all(len(text) < 200_000 for text in corpus)
        history = [{"role": "user", "content": text} for text in corpus]
        estimate = estimate_request_tokens(history)
        assert 80_000 < estimate < 200_000


def test_control_never_summarizes_or_truncates():
    compactor = NoCompaction(context_window=1_000_000)
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 1000}]
    before = [dict(m) for m in messages]
    assert compactor.needs_compaction(messages) is False
    assert compactor.maybe_compact(messages, [], object()) is None
    assert messages == before


def test_unimplemented_solution_cannot_pass_acceptance(tmp_path):
    build_workspace(tmp_path / "config", "config")
    checks = acceptance(SimpleNamespace(__bench_task__="config"), tmp_path / "config")
    assert len([c for c in checks if c["kind"] == "constraint"]) == 5
    assert not all(c["passed"] for c in checks)
