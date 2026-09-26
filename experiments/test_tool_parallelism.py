"""Test benchmark accounting without calling network services or running workloads."""
from benchmark_tool_parallelism import ALL, generate_manifest, p95, summarize


def test_manifest_scale_coverage_and_reproducibility():
    batches = generate_manifest(20260920, 25)
    assert batches == generate_manifest(20260920, 25)
    assert len(batches) == 100
    assert sum(b["size"] for b in batches) == 750
    assert len({c["id"] for b in batches for c in b["calls"]}) == 750
    for size in (2, 4, 8, 16):
        assert sum(b["size"] == size for b in batches) == 25
    for name in ALL:
        assert sum(any(c["name"] == name for c in b["calls"]) for b in batches) >= 10
    for batch in batches:
        assert len(batch["calls"]) == batch["size"]
        assert sum(c["name"].startswith("browser_") for c in batch["calls"]) <= 1


def test_totals_not_average_of_speedups_and_failures_not_counted():
    batches = [{"id": str(i), "size": 2, "calls": []} for i in range(3)]
    rows = []
    for i, (serial, parallel) in enumerate([(100, 50), (10, 10), (500, 1)]):
        for mode, latency in [("serial", serial), ("parallel", parallel)]:
            rows.append({"batch_id": str(i), "repeat": 0, "size": 2, "mode": mode,
                         "latency_ms": latency, "valid": i != 2, "evidence": "same"})
    result = summarize(rows, batches)
    assert result["overall"]["pairs"] == 3
    assert result["overall"]["valid_pairs"] == 2
    assert result["overall"]["speedup"] == 110 / 60
    assert result["successful_runs"] == 4
    rows[1]["evidence"] = "different"
    assert summarize(rows, batches)["overall"]["valid_pairs"] == 1


def test_p95_nearest_rank():
    assert p95(list(range(1, 101))) == 95
    assert p95([1, 2, 3]) == 3
    assert p95([]) is None
