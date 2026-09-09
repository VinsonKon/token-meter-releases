from __future__ import annotations

import json
import unittest

import model_registry
import sync


class AstraPricingTests(unittest.TestCase):
    def test_standard_short_context_rates_and_model_identity(self) -> None:
        model = model_registry.resolve_model("gpt-6-astra")
        self.assertIsNotNone(model)
        self.assertEqual("GPT-6 Astra", model.display_name)
        self.assertEqual("openai", model.provider_id)
        self.assertEqual((10.0, 1.0, 50.0), model.pricing_usd_per_m)

    def test_normalized_and_versioned_ids_use_astra_rates(self) -> None:
        for model_id in (" GPT-6-ASTRA ", "gpt-6-astra-2026-09-09"):
            with self.subTest(model_id=model_id):
                self.assertEqual("gpt-6-astra", model_registry.canonical_model_id(model_id))
                self.assertEqual((10.0, 1.0, 50.0), model_registry.pricing_for_model(model_id))

    def test_unrelated_model_names_are_not_assigned_astra_prices(self) -> None:
        for model_id in ("gpt-6", "gpt-6-astral", "future-model"):
            with self.subTest(model_id=model_id):
                self.assertIsNone(model_registry.pricing_for_model(model_id))

    def test_astra_only_daily_usage_produces_rmb_packet(self) -> None:
        snapshot = {
            "token_scope": "local_daily",
            "model": "gpt-6-astra",
            "cost_tokens_by_model": {
                "gpt-6-astra": {
                    "input_tokens": 1_000_000,
                    "cached_input_tokens": 800_000,
                    "output_tokens": 100_000,
                    "reasoning_output_tokens": 20_000,
                    "total_tokens": 1_100_000,
                },
            },
        }
        # 200k uncached input + 800k cached input + 100k output = $7.80.
        # Reasoning is already part of output, not a second charge.
        self.assertAlmostEqual(7.8, sync.estimate_cost_usd(snapshot))
        cost, source = sync.cost_cny_for_snapshot(snapshot, None, 6.8)
        self.assertAlmostEqual(53.04, cost)
        self.assertEqual("api-equivalent", source)
        self.assertEqual(5304, json.loads(sync.build_packet(snapshot, cost_cny=cost))["c"])

    def test_legacy_split_estimator_also_supports_astra(self) -> None:
        snapshot = {"model": "gpt-6-astra", "token_scope": "local_daily",
                    "cost_tokens": {"input_tokens": 1_000_000, "output_tokens": 0}}
        self.assertEqual(10.0, sync.estimate_cost_usd(snapshot))

    def test_empty_astra_day_is_zero(self) -> None:
        snapshot = {"model": "gpt-6-astra", "token_scope": "local_daily",
                    "cost_tokens_by_model": {"gpt-6-astra": {"total_tokens": 0}}}
        self.assertEqual((0.0, "api-equivalent"), sync.cost_cny_for_snapshot(snapshot, None, 6.8))


if __name__ == "__main__":
    unittest.main()
