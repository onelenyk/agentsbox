"""Tests for core/usage.py — parsing opencode usage + recording + aggregation."""
import json

from core.usage import (
    by_agent,
    init_usage_table,
    parse_usage,
    record_run,
    totals,
)


def _step(cost, inp, out, rea, cache_read=0):
    return json.dumps({"part": {"type": "step-finish", "tokens": {
        "total": inp + out + rea, "input": inp, "output": out, "reasoning": rea,
        "cache": {"write": 0, "read": cache_read}}, "cost": cost}})


class TestParse:
    def test_sums_steps(self):
        out = "\n".join([
            '{"type":"step_start"}',
            _step(0.02, 43552, 52, 93),
            _step(0.0004, 199, 110, 56, cache_read=43648),
        ])
        u = parse_usage(out)
        assert u["steps"] == 2
        assert u["input_tokens"] == 43552 + 199
        assert u["output_tokens"] == 52 + 110
        assert u["reasoning_tokens"] == 93 + 56
        assert u["cache_read"] == 43648
        assert round(u["cost"], 4) == 0.0204
        assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"] + u["reasoning_tokens"]

    def test_no_usage_is_zeros(self):
        u = parse_usage('{"type":"text","content":"hello"}\ngarbage line')
        assert u["steps"] == 0 and u["cost"] == 0.0 and u["total_tokens"] == 0

    def test_empty(self):
        assert parse_usage("")["steps"] == 0


class TestRecordAndAggregate:
    def test_record_and_totals(self, db_path):
        init_usage_table()
        record_run("task-1", "engineer", "deepseek", parse_usage(_step(0.01, 100, 20, 5)), 1200)
        record_run("task-1", "reviewer", "deepseek", parse_usage(_step(0.005, 50, 10, 2)), 800)
        t = totals()
        assert t["runs"] == 2
        assert round(t["cost"], 3) == 0.015
        assert t["tokens"] == (100 + 20 + 5) + (50 + 10 + 2)

    def test_task_usage_rollup(self, db_path):
        from core.usage import task_usage
        init_usage_table()
        record_run("t-a", "engineer", "m", parse_usage(_step(0.03, 100, 20, 5, cache_read=500)), 0)
        record_run("t-a", "reviewer", "m", parse_usage(_step(0.02, 40, 10, 0)), 0)
        record_run("t-b", "engineer", "m", parse_usage(_step(0.99, 999, 99, 9)), 0)
        u = task_usage("t-a")
        assert u["runs"] == 2
        assert round(u["cost"], 3) == 0.05
        assert u["tokens"] == (100 + 20 + 5) + (40 + 10 + 0)
        assert u["cache_read"] == 500
        assert task_usage("t-b")["runs"] == 1  # isolated per task

    def test_empty_run_not_recorded(self, db_path):
        init_usage_table()
        record_run("task-1", "engineer", "m", parse_usage(""), 0)   # no steps → skipped
        assert totals()["runs"] == 0

    def test_by_agent_breakdown(self, db_path):
        init_usage_table()
        record_run("t", "engineer", "m", parse_usage(_step(0.02, 100, 20, 0)), 0)
        record_run("t", "engineer", "m", parse_usage(_step(0.01, 50, 10, 0)), 0)
        record_run("t", "reviewer", "m", parse_usage(_step(0.003, 30, 5, 0)), 0)
        rows = {r["agent"]: r for r in by_agent()}
        assert rows["engineer"]["runs"] == 2
        assert round(rows["engineer"]["cost"], 3) == 0.03
        assert rows["reviewer"]["runs"] == 1
