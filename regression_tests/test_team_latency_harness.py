"""Validate the paid harness offline before trusting its acceptance result."""
import importlib.util
from pathlib import Path


def harness():
    path = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_team_latency.py"
    spec = importlib.util.spec_from_file_location("latency_harness", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_workspace_does_not_pass_private_acceptance(tmp_path):
    bench = harness()
    result = bench.execute_tests(tmp_path, private=True)
    assert result["exit_code"] != 0
    assert result["test_count"] == 0


def test_private_acceptance_accepts_reference_contract(tmp_path):
    (tmp_path / "inventory.py").write_text('''
class Inventory:
    def __init__(self, initial):
        if any(not isinstance(k,str) or not k.strip() or type(v) is not int or v<0
               for k,v in initial.items()): raise ValueError()
        self.data=dict(initial)
    def snapshot(self): return dict(self.data)
    def reserve(self, sku, quantity): return self.change(sku, quantity, -1)
    def release(self, sku, quantity): return self.change(sku, quantity, 1)
    def change(self, sku, qty, sign):
        if type(qty) is not int or qty<=0: raise ValueError()
        value=self.data[sku]+qty*sign
        if value<0: raise ValueError()
        self.data[sku]=value
        return value
''', encoding="utf-8")
    (tmp_path / "report.py").write_text('''
import csv, io
def render_inventory(stock):
    buffer=io.StringIO(newline="")
    writer=csv.writer(buffer, lineterminator="\\n")
    writer.writerow(["sku","quantity"])
    writer.writerows(sorted(stock.items()))
    return buffer.getvalue()
''', encoding="utf-8")
    result = harness().execute_tests(tmp_path, private=True)
    assert result["exit_code"] == 0, result["output"]
    assert result["test_count"] == 9


def test_no_generated_tests_is_explicitly_counted_as_zero(tmp_path):
    result = harness().execute_tests(tmp_path)
    assert result["test_count"] == 0
