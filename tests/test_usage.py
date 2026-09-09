from __future__ import annotations

import unittest

from optiprofiler_evolve.observability.usage import UsageRecord, aggregate_usage


class UsageAccountingTests(unittest.TestCase):
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
