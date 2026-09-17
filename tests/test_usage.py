from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage


class UsageAccountingTests(unittest.TestCase):
    def test_codex_native_usage_counts_tokens_but_does_not_invent_api_calls_or_cost(self):
        from optiprofiler_evolve.observability.usage import read_native_usage
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "stdout"
            trace.write_text('{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":80,"output_tokens":12}}\n')
            usage = read_native_usage(trace, "codex")
            self.assertEqual(usage.input_tokens, 100)
            self.assertEqual(usage.output_tokens, 12)
            self.assertEqual(usage.cache_read_tokens, 80)
            self.assertIsNone(usage.calls)
            self.assertIsNone(usage.cost_usd)
            self.assertTrue(usage.incomplete)
            interrupted = read_native_usage(trace, "codex", complete=False)
            self.assertIsNone(interrupted.output_tokens)
            self.assertEqual(interrupted.lower_bounds["output_tokens"], 12)
            trace.write_text(trace.read_text() + '{truncated\n')
            usage = read_native_usage(trace, "codex")
            self.assertIsNone(usage.input_tokens)
            self.assertEqual(usage.lower_bounds["input_tokens"], 100)

    def test_native_model_evidence_reports_observed_models_not_the_requested_name(self):
        from optiprofiler_evolve.observability.usage import read_native_models
        assistant = '{"type":"assistant","message":{"model":"model-main","content":[]}}\n'
        notice = '{"type":"assistant","message":{"model":"<synthetic>","content":[]}}\n'
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "stdout"
            # The terminal record also names a helper model no assistant message showed.
            trace.write_text(assistant + notice + '{"type":"result","modelUsage":'
                             '{"model-main":{"inputTokens":1},"model-helper":{"inputTokens":1}}}\n')
            self.assertEqual(read_native_models(trace, "claude"), {
                "schema": "native_model_evidence/1", "source": "result_model_usage",
                "observed_models": ["model-helper", "model-main"], "complete": True})
            # A stream cut before its terminal record is partial evidence, never complete.
            trace.write_text(assistant + '{"type":"result","modelUs')
            self.assertEqual(read_native_models(trace, "claude"), {
                "schema": "native_model_evidence/1", "source": "assistant_messages",
                "observed_models": ["model-main"], "complete": False})
            for harness, path in (("codex", trace), ("claude", Path(directory) / "absent")):
                evidence = read_native_models(path, harness)
                self.assertEqual((evidence["source"], evidence["observed_models"],
                                  evidence["complete"]), ("none", [], False))

    def test_aggregate_keeps_missing_values_null_and_marks_incomplete(self) -> None:
        first = UsageRecord.from_values(
            source="provider_gateway",
            calls=1,
            input_tokens=100,
            output_tokens=20,
            cache_read_tokens=5,
            cache_write_tokens=2,
            cost_usd=None,
        )
        second = UsageRecord.from_values(
            source="provider_gateway",
            calls=2,
            input_tokens=None,
            output_tokens=30,
            cache_read_tokens=0,
            cache_write_tokens=0,
            cost_usd=None,
        )

        aggregate = aggregate_usage((first, second))

        self.assertEqual(aggregate.calls, 3)
        self.assertIsNone(aggregate.input_tokens)
        self.assertEqual(aggregate.output_tokens, 50)
        self.assertEqual(aggregate.cache_read_tokens, 5)
        self.assertIsNone(aggregate.cost_usd)
        self.assertTrue(aggregate.incomplete)
        self.assertEqual(
            aggregate.missing_fields,
            ("input_tokens", "cost_usd"),
        )


if __name__ == "__main__":
    unittest.main()
