from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import codex_account
import sync


def rate_result() -> dict[str, object]:
    return {
        "rateLimits": {
            "limitId": "codex_bengalfox",
            "primary": {
                "usedPercent": 99,
                "windowDurationMins": 300,
                "resetsAt": 1,
            },
        },
        # The auxiliary bucket deliberately comes first. Dict order must never
        # affect which quota becomes the device's main quota.
        "rateLimitsByLimitId": {
            "codex_bengalfox": {
                "limitId": "codex_bengalfox",
                "limitName": "Bengalfox",
                "primary": {
                    "usedPercent": 81,
                    "windowDurationMins": 10080,
                    "resetsAt": 2,
                },
                "secondary": None,
                "planType": "model",
            },
            "codex": {
                "limitId": "codex",
                "limitName": None,
                "primary": {
                    "usedPercent": 23,
                    "windowDurationMins": 10080,
                    "resetsAt": 3,
                },
                "secondary": None,
                "planType": "pro",
                "rateLimitReachedType": None,
            },
        },
        "rateLimitResetCredits": {
            "availableCount": 4,
            "credits": [],
        },
    }


def usage_result() -> dict[str, object]:
    return {
        "summary": {
            "lifetimeTokens": (1 << 32) + 987,
            "peakDailyTokens": 45678,
            "longestRunningTurnSec": 540,
            "currentStreakDays": 8,
            "longestStreakDays": 14,
        },
        "dailyUsageBuckets": [
            {"startDate": "2026-07-30", "tokens": 12345},
        ],
    }


class RateLimitNormalizationTests(unittest.TestCase):
    def test_exact_codex_bucket_is_selected_and_auxiliary_is_preserved(self) -> None:
        normalized = codex_account.normalize_rate_limits_result(rate_result())

        self.assertEqual("codex", normalized["selected_limit_id"])
        self.assertEqual(
            "codex", normalized["rate_limits"]["limit_id"]
        )
        self.assertEqual(
            23, normalized["rate_limits"]["primary"]["used_percent"]
        )
        self.assertIsNone(normalized["rate_limits"]["secondary"])
        self.assertEqual(
            {"codex_bengalfox", "codex"},
            set(normalized["rate_limits_by_limit_id"]),
        )
        self.assertEqual(
            81,
            normalized["rate_limits_by_limit_id"]["codex_bengalfox"][
                "primary"
            ]["used_percent"],
        )
        self.assertEqual(4, normalized["reset_count"])

    def test_missing_codex_does_not_fall_back_to_legacy_or_first_bucket(
        self,
    ) -> None:
        value = rate_result()
        value["rateLimits"] = {
            "limitId": "codex",
            "primary": {
                "usedPercent": 1,
                "windowDurationMins": 300,
                "resetsAt": 1,
            },
        }
        value["rateLimitsByLimitId"] = {
            "codex_bengalfox": value["rateLimitsByLimitId"][
                "codex_bengalfox"
            ],
        }

        with self.assertRaises(codex_account.CodexAccountError):
            codex_account.normalize_rate_limits_result(value)

    def test_weekly_primary_and_null_secondary_are_preserved(self) -> None:
        normalized = codex_account.normalize_rate_limits_result(rate_result())
        selected = normalized["rate_limits"]

        self.assertEqual(10080, selected["primary"]["window_minutes"])
        self.assertIsNone(selected["secondary"])

    def test_null_reset_credits_are_unavailable_not_a_cached_count(self) -> None:
        value = rate_result()
        value["rateLimitResetCredits"] = None

        normalized = codex_account.normalize_rate_limits_result(value)

        self.assertFalse(normalized["reset_count_available"])
        self.assertEqual(0, normalized["reset_count"])
        self.assertEqual(
            "unavailable", normalized["reset_count_source"]
        )


class AccountUsageTests(unittest.TestCase):
    def test_lifetime_token_total_remains_uint64_sized(self) -> None:
        normalized = codex_account.normalize_usage_result(usage_result())

        self.assertEqual(
            (1 << 32) + 987, normalized["tokens"]["total_tokens"]
        )
        self.assertEqual(0, normalized["tokens"]["input_tokens"])
        self.assertEqual(
            "2026-07-30",
            normalized["daily_usage_buckets"][0]["start_date"],
        )

    def test_official_reads_are_cached_without_merging_model_buckets(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.methods: list[str] = []

            def request(self, method: str, _params=None):
                self.methods.append(method)
                if method == "account/read":
                    return {
                        "account": {
                            "type": "chatgpt",
                            "email": "user@example.com",
                        },
                    }
                if method == "account/rateLimits/read":
                    return rate_result()
                if method == "account/usage/read":
                    return usage_result()
                raise AssertionError(method)

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "account.json"
            with mock.patch.object(
                codex_account, "_client_for", return_value=client
            ):
                snapshot = codex_account.read_account_snapshot(
                    cache_path, force=True
                )
            persisted = json.loads(cache_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [
                "account/read",
                "account/rateLimits/read",
                "account/usage/read",
            ],
            client.methods,
        )
        self.assertEqual("codex", snapshot["rate_limits"]["limit_id"])
        self.assertEqual(
            "codex_bengalfox",
            snapshot["rate_limits_by_limit_id"]["codex_bengalfox"][
                "limit_id"
            ],
        )
        self.assertEqual(
            "codex", persisted["rate_limits"]["limit_id"]
        )
        self.assertEqual("<default>", persisted["codex_home_key"])
        self.assertNotIn(
            "user@example.com", json.dumps(persisted)
        )

    def test_null_lifetime_retains_last_total_but_marks_it_stale(self) -> None:
        class FakeClient:
            usage = usage_result()

            def request(self, method: str, _params=None):
                if method == "account/read":
                    return {
                        "account": {
                            "type": "chatgpt",
                            "email": "user@example.com",
                        },
                    }
                if method == "account/rateLimits/read":
                    return rate_result()
                if method == "account/usage/read":
                    return self.usage
                raise AssertionError(method)

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "account.json"
            with mock.patch.object(
                codex_account, "_client_for", return_value=client
            ), mock.patch.object(
                codex_account.time, "time", return_value=1_000.0
            ):
                first = codex_account.read_account_snapshot(
                    cache_path, force=True
                )

            client.usage = {"summary": {"lifetimeTokens": None}}
            with mock.patch.object(
                codex_account, "_client_for", return_value=client
            ), mock.patch.object(
                codex_account.time, "time", return_value=1_121.0
            ):
                second = codex_account.read_account_snapshot(
                    cache_path, force=True
                )

        self.assertEqual(
            first["tokens"]["total_tokens"],
            second["tokens"]["total_tokens"],
        )
        self.assertTrue(second["usage_stale"])
        self.assertTrue(second["account_data_stale"])

    def test_account_switch_never_mixes_new_quota_with_old_usage(self) -> None:
        class FakeClient:
            email = "first@example.com"
            fail_usage = False

            def request(self, method: str, _params=None):
                if method == "account/read":
                    return {
                        "account": {
                            "type": "chatgpt",
                            "email": self.email,
                        },
                    }
                if method == "account/rateLimits/read":
                    return rate_result()
                if method == "account/usage/read":
                    if self.fail_usage:
                        raise codex_account.CodexRpcError(
                            "usage unavailable"
                        )
                    return usage_result()
                raise AssertionError(method)

        client = FakeClient()
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "account.json"
            with mock.patch.object(
                codex_account, "_client_for", return_value=client
            ):
                first = codex_account.read_account_snapshot(
                    cache_path, force=True
                )
                client.email = "second@example.com"
                client.fail_usage = True
                second = codex_account.read_account_snapshot(
                    cache_path, force=True
                )

        self.assertTrue(first["tokens"])
        self.assertEqual({}, second["tokens"])
        self.assertNotIn("token_scope", second)
        self.assertTrue(second["usage_stale"])
        self.assertTrue(second["account_data_stale"])


class DevicePacketSelectionTests(unittest.TestCase):
    def snapshot(self) -> dict[str, object]:
        normalized = codex_account.normalize_rate_limits_result(rate_result())
        normalized.update(codex_account.normalize_usage_result(usage_result()))
        normalized.update({
            "provider": "openai",
            "model": "gpt-5.6-sol",
            "activity": "idle",
        })
        return normalized

    def test_weekly_primary_is_encoded_in_week_slot_not_5h_slot(self) -> None:
        message = json.loads(sync.build_packet(self.snapshot()))

        self.assertIsNone(message["q"][0])
        self.assertEqual(10080, message["q"][1][1])
        self.assertEqual((1 << 32) + 987, message["t"][4])
        self.assertTrue(message["lt"])
        self.assertTrue(message["rcv"])

    def test_stale_official_snapshot_is_signalled_to_firmware(self) -> None:
        snapshot = self.snapshot()
        snapshot["account_data_stale"] = True

        message = json.loads(sync.build_packet(snapshot))

        self.assertFalse(message["af"])

    def test_unavailable_reset_count_is_explicit_on_wire(self) -> None:
        snapshot = self.snapshot()
        snapshot["reset_count"] = 0
        snapshot["reset_count_available"] = False
        snapshot["reset_count_source"] = "unavailable"

        message = json.loads(sync.build_packet(snapshot))

        self.assertEqual(0, message["rc"])
        self.assertFalse(message["rcv"])

    def test_quota_below_ten_percent_sets_limit_flag(self) -> None:
        snapshot = self.snapshot()
        snapshot["rate_limits"]["primary"]["used_percent"] = 91

        message = json.loads(sync.build_packet(snapshot))

        self.assertTrue(message["r"])

    def test_exactly_ten_percent_remaining_is_not_limited(self) -> None:
        snapshot = self.snapshot()
        snapshot["rate_limits"]["primary"]["used_percent"] = 90

        message = json.loads(sync.build_packet(snapshot))

        self.assertFalse(message["r"])

    def test_fractional_low_weekly_quota_preserves_affected_window(self) -> None:
        snapshot = self.snapshot()
        snapshot["rate_limits"]["primary"] = {
            "used_percent": 20,
            "window_minutes": 300,
            "resets_at": 1,
            "tokens_used": 0,
        }
        snapshot["rate_limits"]["secondary"] = {
            "used_percent": 90.1,
            "window_minutes": 10080,
            "resets_at": 2,
            "tokens_used": 0,
        }

        message = json.loads(sync.build_packet(snapshot))

        self.assertTrue(message["r"])
        self.assertEqual(20, message["q"][0][0])
        self.assertEqual(91, message["q"][1][0])

    def test_fractional_threshold_quantization_keeps_ten_percent_normal(
        self,
    ) -> None:
        snapshot = self.snapshot()
        snapshot["rate_limits"]["primary"]["used_percent"] = 90.0
        exact = json.loads(sync.build_packet(snapshot))
        snapshot["rate_limits"]["primary"]["used_percent"] = 90.5
        below = json.loads(sync.build_packet(snapshot))

        self.assertFalse(exact["r"])
        self.assertEqual(90, exact["q"][1][0])
        self.assertTrue(below["r"])
        self.assertEqual(91, below["q"][1][0])

    def test_approval_and_low_quota_are_independent_wire_states(self) -> None:
        snapshot = self.snapshot()
        snapshot["rate_limits"]["primary"]["used_percent"] = 91
        snapshot["activity"] = "need approval"

        message = json.loads(sync.build_packet(snapshot))

        self.assertEqual("need approval", message["s"])
        self.assertTrue(message["r"])

    def test_auxiliary_bucket_change_does_not_change_device_packet(self) -> None:
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["rate_limits_by_limit_id"]["codex_bengalfox"]["primary"][
            "used_percent"
        ] = 1

        self.assertEqual(
            sync.selected_quota_signature(before),
            sync.selected_quota_signature(after),
        )
        self.assertEqual(
            sync.packet_signature(sync.build_packet(before)),
            sync.packet_signature(sync.build_packet(after)),
        )
        signature = sync.packet_signature(sync.build_packet(before))
        self.assertFalse(
            sync.should_transmit_packet(
                sync.packet_signature(sync.build_packet(after)),
                signature,
            )
        )

    def test_selected_bucket_change_passes_send_gate(self) -> None:
        before = self.snapshot()
        after = copy.deepcopy(before)
        after["rate_limits"]["primary"]["used_percent"] = 24

        self.assertNotEqual(
            sync.selected_quota_signature(before),
            sync.selected_quota_signature(after),
        )
        self.assertNotEqual(
            sync.packet_signature(sync.build_packet(before)),
            sync.packet_signature(sync.build_packet(after)),
        )
        self.assertTrue(
            sync.should_transmit_packet(
                sync.packet_signature(sync.build_packet(after)),
                sync.packet_signature(sync.build_packet(before)),
            )
        )

    def test_cost_uses_local_daily_split_not_lifetime_total(self) -> None:
        snapshot = self.snapshot()
        snapshot["cost_tokens"] = {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 1_000_000,
        }

        self.assertEqual(5.0, sync.estimate_cost_usd(snapshot))

    def test_empty_local_daily_cost_is_explicit_zero(self) -> None:
        snapshot = self.snapshot()
        snapshot["token_scope"] = "local_daily"
        snapshot["cost_tokens"] = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 0,
        }

        self.assertEqual(0.0, sync.estimate_cost_usd(snapshot))
        cost_cny, source = sync.cost_cny_for_snapshot(snapshot, None, 6.8)
        self.assertEqual((0.0, "api-equivalent"), (cost_cny, source))
        self.assertEqual(
            0,
            json.loads(sync.build_packet(snapshot, cost_cny=cost_cny))["c"],
        )

    def test_cost_prices_models_separately_and_skips_unknown(self) -> None:
        snapshot = self.snapshot()
        snapshot["model"] = "future-model"
        snapshot["cost_tokens_by_model"] = {
            "gpt-5.6-sol": {
                "input_tokens": 1_000_000,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 1_000_000,
            },
            "gpt-5.6-terra": {
                "input_tokens": 1_000_000,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 1_000_000,
            },
            "future-model": {
                "input_tokens": 9_000_000,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 9_000_000,
            },
        }

        self.assertEqual(7.5, sync.estimate_cost_usd(snapshot))

    def test_unknown_only_usage_is_unavailable_not_misleading_zero(self) -> None:
        snapshot = self.snapshot()
        snapshot["cost_tokens_by_model"] = {
            "future-model": {
                "input_tokens": 123,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": 123,
            },
        }

        self.assertIsNone(sync.estimate_cost_usd(snapshot))
        cost_cny, source = sync.cost_cny_for_snapshot(snapshot, None, 6.8)
        message = json.loads(sync.build_packet(snapshot, cost_cny=cost_cny))
        self.assertIsNone(cost_cny)
        self.assertEqual("unavailable", source)
        self.assertEqual(-1, message["c"])


if __name__ == "__main__":
    unittest.main()
