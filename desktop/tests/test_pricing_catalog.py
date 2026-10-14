from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

import model_registry
import pricing_catalog as prices
import sync
import tray_app


# Synthetic prices make tests independent of future commercial price changes.
DOCUMENT = """# Pricing
Flagship models
Standard
### Standard pricing data
| Model | Short context input | Short context cached input | Short context cache writes | Short context output | Long context input | Long context cached input | Long context cache writes | Long context output |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt-next | $3.00 | $0.30 | $3.75 | $15.00 | $6.00 | $0.60 | $7.50 | $22.50 |
| gpt-next-pro (<272K context length) | $30.00 | - | - | $180.00 | $60.00 | - | - | $270.00 |
| gpt-test-a | $1.00 | $0.10 | - | $5.00 | - | - | - | - |
| gpt-test-b | $1.00 | $0.10 | - | $5.00 | - | - | - | - |
| gpt-test-c | $1.00 | $0.10 | - | $5.00 | - | - | - | - |
Batch
### Batch pricing data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-next | $1.50 | $0.15 | $7.50 |
Fast
### Fast pricing data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-next | $6.00 | $0.60 | $30.00 |
Ultrafast
### Ultrafast pricing data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-next | $18.00 | $1.80 | $90.00 |
Short context: ≤272K input tokens. Long context: >272K input tokens.
Cyber models
### Grouped Pricing Table data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-test-cyber | $12.50 | $1.25 | $75.00 |
Life sciences models
### Grouped Pricing Table data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-test-science | $5.00 | $0.50 | $25.00 |
Multimodal models
Standard
### Grouped Pricing Table data
| Model | Modality | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| gpt-test-audio | Audio | $32.00 | $0.40 | $64.00 |
Specialized models
Standard
### Grouped Pricing Table data
| Category | Model | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| Codex | gpt-test-codex | $1.75 | $0.175 | $14.00 |
| Embedding | text-embedding-test | $0.02 | - | - |
| Moderation | moderation-test | Free | - | - |
Finetuning
Standard
### Pricing Table data
| Model | Training | Input | Cached input | Output |
| --- | --- | --- | --- | --- |
| gpt-next | $100.00 / hour | $99.00 | $9.00 | $199.00 |
"""


class Response(io.BytesIO):
    def geturl(self):
        return prices.PRICING_DATA_URL


class PricingCatalogueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.cache = Path(self.temporary.name) / ".model-pricing.json"

    def load_live(self):
        catalogue = prices.PricingCatalogue(self.cache, allow_live=True)
        with mock.patch.object(prices, "urlopen", return_value=Response(DOCUMENT.encode())):
            self.assertEqual("live", catalogue.refresh().source)
        return catalogue

    def test_named_columns_keep_tiers_context_and_cache_writes_separate(self):
        catalogue = self.load_live()
        self.assertEqual(8, catalogue.status().model_count)
        self.assertEqual((3.0, 0.3, 15.0), catalogue.lookup("gpt-next")[1].legacy_tuple())
        self.assertEqual(3.75, catalogue.lookup("gpt-next")[1].cache_write)
        self.assertEqual((6.0, 0.6, 22.5),
                         catalogue.lookup("gpt-next", context_tokens=272001)[1].legacy_tuple())
        self.assertEqual((3.0, 0.3, 15.0),
                         catalogue.lookup("gpt-next", context_tokens=272000)[1].legacy_tuple())
        self.assertEqual((1.5, 0.15, 7.5),
                         catalogue.lookup("gpt-next", tier="batch")[1].legacy_tuple())
        self.assertEqual((6.0, 0.6, 30.0),
                         catalogue.lookup("gpt-next", tier="priority")[1].legacy_tuple())
        self.assertEqual((18.0, 1.8, 90.0),
                         catalogue.lookup("gpt-next", tier="ultrafast")[1].legacy_tuple())
        self.assertEqual(12.5, catalogue.lookup("gpt-test-cyber")[1].input)
        self.assertEqual(5.0, catalogue.lookup("gpt-test-science")[1].input)
        self.assertEqual(1.75, catalogue.lookup("gpt-test-codex")[1].input)
        self.assertIsNone(catalogue.lookup("gpt-test-audio"))
        self.assertIsNone(catalogue.lookup("text-embedding-test"))
        self.assertIsNone(catalogue.lookup("moderation-test"))

    def test_new_online_model_is_used_without_a_registry_entry(self):
        catalogue = self.load_live()
        self.assertNotIn("gpt-next", {m.id for m in model_registry.MODELS})
        snapshot = {"token_scope": "local_daily", "cost_tokens_by_model": {
            "gpt-next-2026-10-13": {"input_tokens": 1_000_000,
                                     "cached_input_tokens": 800_000,
                                     "output_tokens": 100_000},
        }}
        with (mock.patch.object(prices, "active", catalogue),
              mock.patch.object(prices, "urlopen") as download):
            self.assertEqual("gpt-next", model_registry.canonical_model_id("gpt-next-2026-10-13"))
            self.assertAlmostEqual(2.34, sync.estimate_cost_usd(snapshot))
            cost, source = sync.cost_cny_for_snapshot(snapshot, None, 6.8)
            self.assertEqual("api-equivalent", source)
            self.assertEqual(round(2.34 * 6.8 * 100), json.loads(sync.build_packet(snapshot, cost_cny=cost))["c"])
            download.assert_not_called()

    def test_variants_do_not_inherit_base_price_and_null_cache_is_not_free(self):
        catalogue = self.load_live()
        with mock.patch.object(prices, "active", catalogue):
            self.assertEqual((30.0, 30.0, 180.0), model_registry.pricing_for_model("gpt-next-pro"))
            for model in ("gpt-next-mini", "gpt-next-pro-max", "gpt-next-fast",
                          "gpt-5.5-pro", "gpt-6-astra-something"):
                self.assertIsNone(model_registry.pricing_for_model(model))
            self.assertEqual((10.0, 1.0, 50.0),
                             model_registry.pricing_for_model("gpt-6-astra-2026-09-09"))

    def test_restart_uses_cache_without_network_and_refreshes_after_24_hours(self):
        original = self.load_live()
        saved_at = original.status().fetched_at
        with mock.patch.object(prices, "urlopen") as download:
            catalogue = prices.PricingCatalogue(self.cache, allow_live=True)
            self.assertEqual("cache", catalogue.status().source)
            self.assertFalse(catalogue.refresh_in_background())
            self.assertEqual(3.0, catalogue.lookup("gpt-next")[1].input)
            catalogue.refresh()
            download.assert_not_called()
        with (mock.patch.object(prices.time, "time", return_value=saved_at + prices.REFRESH_SECONDS + 1),
              mock.patch.object(prices, "urlopen", return_value=Response(DOCUMENT.replace("$3.00", "$4.00").encode()))):
            self.assertEqual("stale-cache", catalogue.status().source)
            self.assertEqual("live", catalogue.refresh().source)
            self.assertEqual(4.0, catalogue.lookup("gpt-next")[1].input)

    def test_failed_download_keeps_cache_and_throttles_retries(self):
        catalogue = self.load_live()
        original_bytes = self.cache.read_bytes()
        saved_at = catalogue.status().fetched_at
        with (mock.patch.object(prices.time, "time", return_value=saved_at + prices.REFRESH_SECONDS + 1),
              mock.patch.object(prices, "urlopen", side_effect=TimeoutError("offline")) as download):
            result = catalogue.refresh()
            self.assertEqual("stale-cache", result.source)
            self.assertEqual("offline", result.error)
            self.assertEqual(3.0, catalogue.lookup("gpt-next")[1].input)
            self.assertFalse(catalogue.refresh_in_background())
            catalogue.refresh()
            self.assertEqual(1, download.call_count)
        self.assertEqual(original_bytes, self.cache.read_bytes())

    def test_bad_html_bad_price_partial_and_oversize_never_overwrite_cache(self):
        catalogue = self.load_live()
        original_bytes = self.cache.read_bytes()
        for document in ("<html>temporary error</html>",
                         DOCUMENT.replace("$3.00", "$NaN"),
                         DOCUMENT.replace("$3.00 | $0.30", "$3.00"),
                         DOCUMENT[:DOCUMENT.index("| gpt-test-a")],
                         "x" * (prices.MAX_DOCUMENT_BYTES + 1)):
            with self.subTest(length=len(document)):
                with mock.patch.object(prices, "urlopen", return_value=Response(document.encode())):
                    result = catalogue.refresh(force=True)
                self.assertTrue(result.error)
                self.assertEqual(3.0, catalogue.lookup("gpt-next")[1].input)
                self.assertEqual(original_bytes, self.cache.read_bytes())

    def test_corrupt_nan_future_or_wrong_source_cache_is_ignored(self):
        self.load_live()
        baseline = json.loads(self.cache.read_text())
        invalid_values = ["{", json.dumps({**baseline, "source": "https://unrelated.test"}),
                          json.dumps({**baseline, "fetched_at": time.time() + 6000}),
                          json.dumps({**baseline, "fetched_at": float("nan")}),
                          json.dumps({**baseline, "models": {"gpt-next": {
                              "standard": {"short": {"input": float("nan"), "output": 1}}}}})]
        for value in invalid_values:
            with self.subTest(value=value[:80]):
                self.cache.write_text(value, encoding="utf-8")
                catalogue = prices.PricingCatalogue(self.cache)
                self.assertEqual("builtin", catalogue.status().source)
                self.assertIsNone(catalogue.lookup("gpt-next"))

    def test_cache_write_failure_still_uses_good_download_in_memory(self):
        catalogue = prices.PricingCatalogue(self.cache, allow_live=True)
        with (mock.patch.object(prices, "urlopen", return_value=Response(DOCUMENT.encode())),
              mock.patch.object(Path, "open", side_effect=PermissionError("read-only")) as create):
            result = catalogue.refresh()
            self.assertEqual(1, create.call_count)
        self.assertEqual("live", result.source)
        self.assertIn("read-only", result.error)
        self.assertEqual(3.0, catalogue.lookup("gpt-next")[1].input)

    def test_background_download_does_not_block_cost_and_notifies_when_ready(self):
        catalogue = prices.PricingCatalogue(self.cache, allow_live=True)
        entered, release, completed = threading.Event(), threading.Event(), threading.Event()
        catalogue.callback = lambda status: completed.set()

        def download(*args, **kwargs):
            entered.set()
            release.wait(3)
            return Response(DOCUMENT.encode())

        with (mock.patch.object(prices, "active", catalogue),
              mock.patch.object(prices, "urlopen", side_effect=download) as request):
            try:
                self.assertTrue(catalogue.refresh_in_background())
                self.assertTrue(entered.wait(2))
                snapshot = {"model": "gpt-6.1-sol", "cost_tokens": {
                    "input_tokens": 1_000_000, "cached_input_tokens": 800_000,
                    "output_tokens": 100_000}}
                self.assertAlmostEqual(1.48 * 6.8, sync.cost_cny_for_snapshot(snapshot, None, 6.8)[0])
                self.assertFalse(catalogue.refresh_in_background(force=True))
                self.assertEqual(1, request.call_count)
            finally:
                release.set()
            self.assertTrue(completed.wait(2))
            self.assertEqual(3.0, catalogue.lookup("gpt-next")[1].input)

    def test_offline_flag_zero_day_unknown_and_manual_cost(self):
        catalogue = prices.PricingCatalogue(self.cache, allow_live=False)
        with (mock.patch.object(prices, "active", catalogue),
              mock.patch.object(prices, "urlopen") as download):
            catalogue.refresh(force=True)
            self.assertFalse(catalogue.refresh_in_background(force=True))
            self.assertEqual((0.0, "api-equivalent"), sync.cost_cny_for_snapshot(
                {"token_scope": "local_daily", "cost_tokens_by_model": {}}, None, 6.8))
            unknown = {"cost_tokens_by_model": {"unknown-model": {"input_tokens": 100}}}
            self.assertEqual((None, "unavailable"), sync.cost_cny_for_snapshot(unknown, None, 6.8))
            self.assertEqual((123.0, "manual-cost"), sync.cost_cny_for_snapshot(unknown, 123, 6.8))
            download.assert_not_called()
        self.assertTrue(sync.parse_args(["--no-live-prices"]).no_live_prices)

    def test_verified_offline_floor_has_latest_models_and_revised_old_rates(self):
        expected = {"gpt-6.1-sol": (2.0, 0.1, 10.0),
                    "gpt-6-sol": (2.0, 0.2, 10.0), "gpt-6-luna": (0.1, 0.01, 0.5),
                    "gpt-5.6-sol": (4.0, 0.4, 20.0),
                    "gpt-5.6-terra": (2.0, 0.2, 12.0), "gpt-5.6-luna": (0.2, 0.02, 1.2)}
        with mock.patch.object(prices, "active", prices.PricingCatalogue()):
            for model, rates in expected.items():
                self.assertEqual(rates, model_registry.pricing_for_model(model))

    def test_tray_price_menu_and_manual_refresh_use_shared_catalogue(self):
        catalogue = self.load_live()
        service = tray_app.TrayService.__new__(tray_app.TrayService)
        service._set_status = mock.Mock()
        with (mock.patch.object(prices, "active", catalogue),
              mock.patch.object(catalogue, "refresh_in_background", return_value=True) as refresh):
            items = list(service._pricing_menu_items())
            self.assertIn("官方在线", items[0].text)
            self.assertEqual("立即更新价格表", items[-1].text)
            service._refresh_prices(None, None)
            refresh.assert_called_once_with(force=True)
            service._set_status.assert_called_once_with("正在更新官方价格表…")

    def test_packed_client_price_validation_does_not_start_live_service(self):
        with (mock.patch.object(tray_app.sys, "argv", ["worker", "--validate-pricing", "public.md"]),
              mock.patch.object(Path, "read_text", return_value=DOCUMENT),
              mock.patch.object(tray_app, "acquire_single_instance") as instance,
              mock.patch.object(tray_app, "TrayService") as service,
              mock.patch("builtins.print") as output):
            self.assertEqual(0, tray_app.main())
            self.assertEqual(8, json.loads(output.call_args[0][0])["model_count"])
            instance.assert_not_called()
            service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
