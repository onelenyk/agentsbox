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


class TestParseClaude:
    """Claude Code's `-p --output-format json` emits one cumulative object per
    run, not opencode's per-step NDJSON stream."""

    def _result(self, **over):
        obj = {
            "type": "result", "subtype": "success", "num_turns": 3,
            "total_cost_usd": 0.068638, "result": "done",
            "usage": {
                "input_tokens": 2, "output_tokens": 4,
                "cache_creation_input_tokens": 6052,
                "cache_read_input_tokens": 16016,
                "output_tokens_details": {"thinking_tokens": 7},
            },
        }
        obj.update(over)
        return json.dumps(obj)

    def test_maps_the_cumulative_block(self):
        u = parse_usage(self._result())
        assert u["input_tokens"] == 2
        assert u["output_tokens"] == 4
        assert u["reasoning_tokens"] == 7
        assert u["cache_write"] == 6052
        assert u["cache_read"] == 16016
        assert round(u["cost"], 6) == 0.068638
        assert u["steps"] == 3
        assert u["total_tokens"] == 2 + 4 + 7

    def test_reports_the_served_model(self):
        """A claude-cli runner row usually names no model, so the run's model
        has to come from what the CLI says it actually served."""
        u = parse_usage(self._result(modelUsage={"claude-opus-5[1m]": {"costUSD": 0.07}}))
        assert u["model"] == "claude-opus-5[1m]"

    def test_missing_model_usage_is_blank_not_an_error(self):
        assert parse_usage(self._result())["model"] == ""

    def test_counts_at_least_one_step(self):
        # record_run drops rows with steps == 0, so a run that reported real
        # tokens must never be silently discarded for want of num_turns.
        u = parse_usage(self._result(num_turns=0))
        assert u["steps"] == 1

    def test_non_result_object_falls_through_to_ndjson_path(self):
        u = parse_usage('{"type":"system","subtype":"init"}')
        assert u["steps"] == 0

    def test_malformed_json_is_zeros(self):
        assert parse_usage('{"type":"result", oops')["steps"] == 0

    def test_finds_result_at_the_end_of_a_stream(self):
        # The worker runs --output-format stream-json, so the result event is
        # the last of many lines rather than the whole of stdout.
        stream = "\n".join([
            '{"type":"system","subtype":"init"}',
            '{"type":"assistant","message":{"content":"working"}}',
            '{"type":"user","message":{"content":"tool result"}}',
            self._result(),
        ])
        u = parse_usage(stream)
        assert u["input_tokens"] == 2
        assert u["steps"] == 3
        assert round(u["cost"], 6) == 0.068638

    def test_last_result_wins(self):
        stream = "\n".join([
            self._result(num_turns=1, total_cost_usd=0.01),
            self._result(num_turns=9, total_cost_usd=0.5),
        ])
        u = parse_usage(stream)
        assert u["steps"] == 9
        assert round(u["cost"], 3) == 0.5

    def test_stream_without_result_event_is_zeros(self):
        # A crash mid-run leaves events but no terminal result.
        u = parse_usage('{"type":"system"}\n{"type":"assistant"}')
        assert u["steps"] == 0


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
