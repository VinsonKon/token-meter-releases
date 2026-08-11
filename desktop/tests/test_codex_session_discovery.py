from __future__ import annotations

import asyncio
import copy
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

import sync
import watchfiles


def _event(payload: dict[str, object]) -> str:
    return json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "event_msg",
        "payload": payload,
    }) + "\n"


def _response(payload: dict[str, object]) -> str:
    return json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": "response_item",
        "payload": payload,
    }) + "\n"


def _record(
    record_type: str,
    payload: dict[str, object],
    timestamp: str | None = None,
) -> str:
    return json.dumps({
        "timestamp": timestamp or datetime.now(timezone.utc).isoformat(),
        "type": record_type,
        "payload": payload,
    }, sort_keys=True) + "\n"


def _session_meta(
    *, thread_source: str = "user", source: object = "vscode"
) -> str:
    return _record("session_meta", {
        "thread_source": thread_source,
        "source": source,
    })


def _turn_context(model: str) -> str:
    return _record("turn_context", {"model": model})


def _token_count(
    total: int,
    *,
    input_tokens: int | None = None,
    output_tokens: int = 0,
    timestamp: str | None = None,
) -> str:
    input_value = total - output_tokens if input_tokens is None else input_tokens
    return _record("event_msg", {
        "type": "token_count",
        "info": {
            "total_token_usage": {
                "input_tokens": input_value,
                "cached_input_tokens": 0,
                "output_tokens": output_tokens,
                "reasoning_output_tokens": 0,
                "total_tokens": total,
            },
        },
    }, timestamp)


def _official_snapshot() -> dict[str, object]:
    return {
        "selected_limit_id": "codex",
        "rate_limits": {
            "limit_id": "codex",
            "primary": {
                "used_percent": 20,
                "window_minutes": 10080,
                "resets_at": int(datetime.now().timestamp()) + 3600,
                "tokens_used": 0,
            },
            "secondary": None,
            "plan_type": "pro",
            "rate_limit_reached_type": None,
        },
        "rate_limits_by_limit_id": {
            "codex": {"limit_id": "codex"},
        },
        "tokens": {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": 9_999_999,
        },
        "token_scope": "account_lifetime",
        "reset_count": 0,
        "reset_count_available": False,
        "reset_count_source": "unavailable",
        "account_source": "Codex App Server",
    }


class CodexApprovalStateTests(unittest.TestCase):
    def test_escalated_call_enters_and_output_leaves_approval(self) -> None:
        call = _response({
            "type": "function_call",
            "name": "shell_command",
            "call_id": "call-1",
            "arguments": json.dumps({
                "command": "example",
                "sandbox_permissions": "require_escalated",
            }),
        })

        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            call,
        ])
        self.assertTrue(active)
        self.assertTrue(needs_approval)

        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            call,
            _response({
                "type": "function_call_output",
                "call_id": "call-1",
                "output": "done",
            }),
        ])
        self.assertTrue(active)
        self.assertFalse(needs_approval)

    def test_concurrent_approval_calls_clear_independently(self) -> None:
        calls = [
            _response({
                "type": "function_call",
                "name": "shell_command",
                "call_id": call_id,
                "arguments": {
                    "sandbox_permissions": "require_escalated",
                },
            })
            for call_id in ("call-1", "call-2")
        ]
        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            *calls,
            _response({
                "type": "function_call_output",
                "call_id": "call-1",
            }),
        ])

        self.assertTrue(active)
        self.assertTrue(needs_approval)

    def test_cancelled_or_failed_task_clears_pending_approval(self) -> None:
        call = _response({
            "type": "function_call",
            "name": "shell_command",
            "call_id": "call-1",
            "arguments": {
                "sandbox_permissions": "require_escalated",
            },
        })
        for terminal in sync.CODEX_TASK_ABORT_EVENTS:
            with self.subTest(terminal=terminal):
                active, needs_approval, _ = (
                    sync._codex_session_runtime_state([
                        _event({"type": "task_started"}),
                        call,
                        _event({"type": terminal}),
                    ])
                )
                self.assertFalse(active)
                self.assertFalse(needs_approval)

    def test_approval_detection_does_not_search_nested_or_plain_text(self) -> None:
        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            _response({
                "type": "function_call",
                "name": "shell_command",
                "call_id": "call-1",
                "arguments": json.dumps({
                    "command": (
                        "echo sandbox_permissions=require_escalated"
                    ),
                    "metadata": {
                        "sandbox_permissions": "require_escalated",
                    },
                }),
            }),
        ])

        self.assertTrue(active)
        self.assertFalse(needs_approval)

    def test_modern_exec_call_enters_and_output_leaves_approval(self) -> None:
        call = _response({
            "type": "custom_tool_call",
            "name": "exec",
            "call_id": "call-modern",
            "status": "completed",
            "input": """const result = await tools.exec_command({
                cmd: "example",
                sandbox_permissions: "require_escalated",
                justification: "Allow this action?",
            });
            text(result);""",
        })

        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            call,
        ])
        self.assertTrue(active)
        self.assertTrue(needs_approval)

        active, needs_approval, _ = sync._codex_session_runtime_state([
            _event({"type": "task_started"}),
            call,
            _response({
                "type": "custom_tool_call_output",
                "call_id": "call-modern",
                "output": "done",
            }),
        ])
        self.assertTrue(active)
        self.assertFalse(needs_approval)

    def test_modern_shell_command_remains_supported(self) -> None:
        self.assertTrue(
            sync._codex_exec_input_requires_approval({
                "type": "custom_tool_call",
                "name": "exec",
                "input": """const result = await tools.shell_command({
                    command: "example",
                    sandbox_permissions: "require_escalated",
                });
                text(result);""",
            })
        )



    def test_modern_exec_approval_detection_is_scoped_to_shell_options(
        self,
    ) -> None:
        inputs = (
            """const result = await tools.exec_command({
                cmd: 'echo sandbox_permissions: "require_escalated"',
            }); text(result);""",
            """const value = {
                sandbox_permissions: "require_escalated",
            }; text(value);""",
            """const result = await tools.shell_command({
                command: "example",
                metadata: {
                    sandbox_permissions: "require_escalated",
                },
            }); text(result);""",
        )

        for index, source in enumerate(inputs):
            with self.subTest(index=index):
                active, needs_approval, _ = (
                    sync._codex_session_runtime_state([
                        _event({"type": "task_started"}),
                        _response({
                            "type": "custom_tool_call",
                            "name": "exec",
                            "call_id": f"call-{index}",
                            "status": "completed",
                            "input": source,
                        }),
                    ])
                )
                self.assertTrue(active)
                self.assertFalse(needs_approval)

    def test_approval_takes_priority_over_thinking_activity(self) -> None:
        snapshot = {
            "task_active": True,
            "needs_approval": True,
            "activity": "idle",
        }

        packet = json.loads(sync.build_packet(snapshot))
        sync._refresh_activity(snapshot)

        self.assertEqual("need approval", packet["s"])
        self.assertTrue(packet["ap"])
        self.assertEqual("need approval", snapshot["activity"])

    def test_approval_is_aggregated_across_concurrent_sessions(self) -> None:
        sync._codex_caches.clear()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".codex"
            today = datetime.now().astimezone()
            sessions = (
                home / "sessions" / today.strftime("%Y")
                / today.strftime("%m") / today.strftime("%d")
            )
            sessions.mkdir(parents=True)
            waiting = sessions / "waiting.jsonl"
            thinking = sessions / "thinking.jsonl"
            waiting.write_text(
                _event({"type": "task_started"})
                + _response({
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({
                        "sandbox_permissions": "require_escalated",
                    }),
                }),
                encoding="utf-8",
            )
            thinking.write_text(
                _event({"type": "task_started"}),
                encoding="utf-8",
            )

            active, needs_approval, _ = sync.read_codex_runtime_state(home)
            packet = json.loads(sync.build_packet({
                "task_active": active,
                "needs_approval": needs_approval,
                "activity": "thinking",
            }))

            self.assertTrue(active)
            self.assertTrue(needs_approval)
            self.assertEqual("need approval", packet["s"])
            self.assertTrue(packet["ap"])
        sync._codex_caches.clear()


class CodexSessionFilteringAndTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        sync._codex_caches.clear()
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / ".codex"
        today = datetime.now().astimezone()
        self.sessions = (
            self.home / "sessions" / today.strftime("%Y")
            / today.strftime("%m") / today.strftime("%d")
        )
        self.sessions.mkdir(parents=True)
        self.account_reader = mock.patch.object(
            sync.codex_account,
            "read_account_snapshot",
            side_effect=lambda *_args, **_kwargs: copy.deepcopy(
                _official_snapshot()
            ),
        )
        self.account_reader.start()

    def tearDown(self) -> None:
        self.account_reader.stop()
        sync._codex_caches.clear()
        self.temporary.cleanup()

    def test_guardian_is_excluded_from_state_model_and_tokens(self) -> None:
        user = self.sessions / "user.jsonl"
        guardian = self.sessions / "guardian.jsonl"
        user.write_text(
            _session_meta()
            + _turn_context("gpt-5.6-sol")
            + _event({"type": "task_started"})
            + _token_count(100),
            encoding="utf-8",
        )
        guardian.write_text(
            _session_meta(
                thread_source="subagent",
                source={"subagent": {"other": "guardian"}},
            )
            + _turn_context("unknown-guardian-model")
            + _event({"type": "task_started"})
            + _response({
                "type": "function_call",
                "name": "shell_command",
                "call_id": "guardian-call",
                "arguments": {
                    "sandbox_permissions": "require_escalated",
                },
            })
            + _token_count(10_000),
            encoding="utf-8",
        )

        snapshot = sync._read_codex_usage(self.home)
        cache = sync._codex_cache(self.home)

        self.assertEqual("thinking", snapshot["activity"])
        self.assertFalse(snapshot["needs_approval"])
        self.assertEqual("gpt-5.6-sol", snapshot["model"])
        self.assertEqual(user.resolve(), Path(snapshot["source"]))
        self.assertEqual(100, snapshot["tokens"]["total_tokens"])
        self.assertEqual("local_daily", snapshot["token_scope"])
        self.assertEqual(
            9_999_999, snapshot["account_tokens"]["total_tokens"]
        )
        self.assertEqual(
            {"gpt-5.6-sol"}, set(snapshot["cost_tokens_by_model"])
        )
        self.assertFalse(cache.states[guardian.resolve()].trackable)
        self.assertNotIn(guardian.resolve(), cache.candidates)

    def test_cold_start_finds_today_modified_user_session_in_old_folder(
        self,
    ) -> None:
        internal = self.sessions / "internal.jsonl"
        internal.write_text(
            _session_meta(thread_source="subagent")
            + _turn_context("gpt-5.6-sol")
            + _token_count(10_000),
            encoding="utf-8",
        )
        old_folder = self.home / "sessions" / "2000" / "01" / "01"
        old_folder.mkdir(parents=True)
        user = old_folder / "resumed-user.jsonl"
        user.write_text(
            _session_meta()
            + _turn_context("gpt-5.6-sol")
            + _token_count(321),
            encoding="utf-8",
        )

        snapshot = sync._read_codex_usage(self.home)
        cache = sync._codex_cache(self.home)

        self.assertEqual(321, snapshot["tokens"]["total_tokens"])
        self.assertIn(user.resolve(), cache.candidates)
        self.assertFalse(cache.states[internal.resolve()].trackable)

    def test_internal_sessions_do_not_consume_runtime_scan_limit(self) -> None:
        user = self.sessions / "00-user.jsonl"
        user.write_text(
            _session_meta() + _event({"type": "task_started"}),
            encoding="utf-8",
        )
        old_epoch = datetime.now().timestamp() - 120
        os.utime(user, (old_epoch, old_epoch))
        for index in range(sync.CODEX_TASK_SESSION_SCAN_LIMIT + 1):
            internal = self.sessions / f"99-internal-{index:02d}.jsonl"
            internal.write_text(
                _session_meta(
                    thread_source="subagent",
                    source={
                        "subagent": {
                            "thread_spawn": {"agent_path": f"/root/{index}"},
                        },
                    },
                )
                + _event({"type": "task_started"}),
                encoding="utf-8",
            )

        active, needs_approval, _ = sync.read_codex_runtime_state(self.home)

        self.assertTrue(active)
        self.assertFalse(needs_approval)
        self.assertEqual([user.resolve()], sync._codex_cache(self.home).candidates)

    def test_cumulative_replay_does_not_move_baseline_backwards(self) -> None:
        session = self.sessions / "replay.jsonl"
        stamp_a = datetime.now(timezone.utc).isoformat()
        event_a = _token_count(100, timestamp=stamp_a)
        session.write_text(
            _session_meta()
            + _turn_context("gpt-5.6-sol")
            + event_a
            + _token_count(150)
            + event_a
            + _token_count(200),
            encoding="utf-8",
        )

        snapshot = sync._read_codex_usage(self.home)

        self.assertEqual(200, snapshot["tokens"]["total_tokens"])
        self.assertEqual(200, snapshot["cost_tokens"]["total_tokens"])

    def test_cumulative_counter_reset_counts_new_baseline(self) -> None:
        session = self.sessions / "reset.jsonl"
        session.write_text(
            _session_meta()
            + _turn_context("gpt-5.6-sol")
            + _token_count(100)
            + _token_count(150)
            + _token_count(10)
            + _token_count(30),
            encoding="utf-8",
        )

        snapshot = sync._read_codex_usage(self.home)

        self.assertEqual(180, snapshot["tokens"]["total_tokens"])

    def test_truncated_session_rebuilds_global_event_fingerprints(self) -> None:
        session = self.sessions / "truncated.jsonl"
        prefix = _session_meta() + _turn_context("gpt-5.6-sol")
        first = _token_count(100)
        session.write_text(
            prefix + first + _token_count(200),
            encoding="utf-8",
        )
        self.assertEqual(
            200,
            sync._read_codex_usage(self.home)["tokens"]["total_tokens"],
        )

        session.write_text(prefix + first, encoding="utf-8")
        rebuilt = sync._read_codex_usage(self.home)
        self.assertEqual(100, rebuilt["tokens"]["total_tokens"])

        with session.open("a", encoding="utf-8") as stream:
            stream.write(_token_count(250))
        continued = sync._read_codex_usage(self.home)
        self.assertEqual(250, continued["tokens"]["total_tokens"])

    def test_exact_events_copied_across_files_are_counted_once(self) -> None:
        original = self.sessions / "original.jsonl"
        continuation = self.sessions / "continuation.jsonl"
        event_a = _token_count(
            100, timestamp=datetime.now(timezone.utc).isoformat()
        )
        event_b = _token_count(
            150, timestamp=datetime.now(timezone.utc).isoformat()
        )
        event_c = _token_count(
            200, timestamp=datetime.now(timezone.utc).isoformat()
        )
        prefix = _session_meta() + _turn_context("gpt-5.6-sol")
        continuation.write_text(
            prefix + event_a + event_b + event_c,
            encoding="utf-8",
        )
        original.write_text(
            prefix + event_a + event_b,
            encoding="utf-8",
        )
        now = datetime.now().timestamp()
        os.utime(continuation, (now - 10, now - 10))
        os.utime(original, (now, now))

        snapshot = sync._read_codex_usage(self.home)

        self.assertEqual(200, snapshot["tokens"]["total_tokens"])

    def test_all_models_total_and_unknown_model_cost_are_independent(self) -> None:
        known = self.sessions / "known.jsonl"
        unknown = self.sessions / "unknown.jsonl"
        known.write_text(
            _session_meta()
            + _turn_context("gpt-5.6-sol")
            + _token_count(1_000_000),
            encoding="utf-8",
        )
        unknown.write_text(
            _session_meta()
            + _turn_context("future-model")
            + _token_count(2_000_000),
            encoding="utf-8",
        )

        snapshot = sync._read_codex_usage(self.home)
        message = json.loads(sync.build_packet(snapshot))

        self.assertEqual(3_000_000, snapshot["tokens"]["total_tokens"])
        self.assertEqual(3_000_000, message["t"][4])
        self.assertEqual([0, 0, 0, 0], message["t"][:4])
        self.assertFalse(message["lt"])
        self.assertEqual(5.0, sync.estimate_cost_usd(snapshot))
        self.assertEqual(
            {"gpt-5.6-sol", "future-model"},
            set(snapshot["cost_tokens_by_model"]),
        )

    def test_auto_source_does_not_hide_codex_approval(self) -> None:
        codex = {
            "source_id": sync.source_registry.CODEX_SOURCE,
            "latest_epoch": 1.0,
            "needs_approval": True,
        }
        claude = {
            "source_id": sync.source_registry.CLAUDE_CODE_SOURCE,
            "latest_epoch": 2.0,
        }
        with (
            mock.patch.object(
                sync, "_read_codex_usage", return_value=codex
            ),
            mock.patch.object(
                sync, "_read_claude_code_usage", return_value=claude
            ),
        ):
            selected = sync.read_latest_usage(
                Path("codex"),
                sync.source_registry.AUTO_SOURCE,
                Path("claude"),
            )

        self.assertIs(codex, selected)

    def test_orphan_timeout_clears_cached_approval_calls(self) -> None:
        sync._codex_caches.clear()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / ".codex"
            today = datetime.now().astimezone()
            sessions = (
                home / "sessions" / today.strftime("%Y")
                / today.strftime("%m") / today.strftime("%d")
            )
            sessions.mkdir(parents=True)
            waiting = sessions / "orphan.jsonl"
            old_event = {
                "timestamp": "2020-01-01T00:00:00+00:00",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "shell_command",
                    "call_id": "call-1",
                    "arguments": json.dumps({
                        "sandbox_permissions": "require_escalated",
                    }),
                },
            }
            waiting.write_text(
                json.dumps(old_event) + "\n",
                encoding="utf-8",
            )
            old_epoch = datetime(
                2020, 1, 1, tzinfo=timezone.utc
            ).timestamp()
            os.utime(waiting, (old_epoch, old_epoch))

            active, needs_approval, _ = sync.read_codex_runtime_state(home)
            state = sync._codex_cache(home).states[waiting.resolve()]

            self.assertFalse(active)
            self.assertFalse(needs_approval)
            self.assertFalse(state.open_calls)
            self.assertFalse(state.approval_calls)
        sync._codex_caches.clear()


class ResumedCodexSessionDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        sync._codex_caches.clear()
        self.account_snapshot = {
            "selected_limit_id": "codex",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {
                    "used_percent": 33,
                    "window_minutes": 10080,
                    "resets_at": int(datetime.now().timestamp()) + 7200,
                    "tokens_used": 0,
                },
                "secondary": None,
                "plan_type": "pro",
                "rate_limit_reached_type": None,
            },
            "rate_limits_by_limit_id": {
                "codex": {"limit_id": "codex"},
                "codex_bengalfox": {
                    "limit_id": "codex_bengalfox",
                    "primary": {
                        "used_percent": 88,
                        "window_minutes": 10080,
                        "resets_at": 1,
                        "tokens_used": 0,
                    },
                    "secondary": None,
                },
            },
            "tokens": {
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
                "total_tokens": (1 << 32) + 123,
            },
            "reset_count": 2,
            "reset_count_source": "account/rateLimits/read",
            "account_source": "Codex App Server",
        }
        self.account_reader = mock.patch.object(
            sync.codex_account,
            "read_account_snapshot",
            side_effect=lambda *_args, **_kwargs: copy.deepcopy(
                self.account_snapshot
            ),
        )
        self.account_reader.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / ".codex"
        self.session = (
            self.home / "sessions" / "2026" / "01" / "01" / "old.jsonl"
        )
        self.session.parent.mkdir(parents=True)
        self.session.write_text(
            _event({
                "type": "token_count",
                "rate_limits": {
                    "primary": {
                        "used_percent": 12,
                        "window_minutes": 10080,
                        "resets_at": int(datetime.now().timestamp()) + 3600,
                    },
                    "secondary": None,
                },
                "info": {"total_token_usage": {"total_tokens": 100}},
            }),
            encoding="utf-8",
        )
        cache = sync._codex_cache(self.home)
        old_time = cache.day_start - 3600
        os.utime(self.session, (old_time, old_time))

        # Cold start sees the newest historical session once. The following
        # hot discovery drops it because it is idle and was last modified
        # before today, reproducing the background client's stuck state.
        sync._refresh_codex_cache(self.home)
        cache.last_discovery = 0.0
        sync._refresh_codex_cache(self.home)
        self.assertEqual([], cache.candidates)

    def tearDown(self) -> None:
        self.account_reader.stop()
        sync._codex_caches.clear()
        self.temporary.cleanup()

    def _resume_session(self) -> None:
        with self.session.open("a", encoding="utf-8") as stream:
            stream.write(_event({"type": "task_started"}))

    def test_dirty_old_session_reenters_candidates(self) -> None:
        self._resume_session()
        sync._mark_codex_path_dirty(self.home, self.session)

        snapshot = sync._read_codex_usage(self.home)
        cache = sync._codex_cache(self.home)

        self.assertEqual(self.session.resolve(), Path(snapshot["source"]))
        self.assertEqual("thinking", snapshot["activity"])
        self.assertEqual(33, snapshot["rate_limits"]["primary"]["used_percent"])
        self.assertIsNone(snapshot["rate_limits"]["secondary"])
        self.assertEqual(100, snapshot["tokens"]["total_tokens"])
        self.assertEqual(
            (1 << 32) + 123,
            snapshot["account_tokens"]["total_tokens"],
        )
        self.assertEqual(100, snapshot["cost_tokens"]["total_tokens"])
        self.assertIn(self.session.resolve(), cache.candidates)
        self.assertNotIn(self.session.resolve(), cache.dirty_paths)

    def test_changed_known_session_recovers_if_watch_event_is_missed(self) -> None:
        self._resume_session()

        snapshot = sync._read_codex_usage(self.home)

        self.assertEqual(self.session.resolve(), Path(snapshot["source"]))
        self.assertEqual("thinking", snapshot["activity"])
        self.assertEqual(33, snapshot["rate_limits"]["primary"]["used_percent"])


class WatcherLifecycleTests(unittest.TestCase):
    def test_watcher_cancel_does_not_stop_sync_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_home = root / ".codex"
            claude_home = root / ".claude"
            (codex_home / "sessions").mkdir(parents=True)

            async def scenario() -> None:
                service_stop_event = threading.Event()
                changed = asyncio.Event()
                watcher_started = asyncio.Event()
                captured_options: dict[str, object] = {}

                async def fake_awatch(*_paths, **options):
                    captured_options.update(options)
                    watcher_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        supplied_stop = options.get("stop_event")
                        if supplied_stop is not None:
                            supplied_stop.set()
                    if False:
                        yield set()

                with mock.patch.object(
                    watchfiles, "awatch", fake_awatch
                ):
                    task = asyncio.create_task(
                        sync.watch_usage_files(
                            codex_home,
                            claude_home,
                            changed,
                            service_stop_event,
                            0.5,
                            None,
                        )
                    )
                    await asyncio.wait_for(
                        watcher_started.wait(), timeout=1.0
                    )
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

                self.assertNotIn("stop_event", captured_options)
                self.assertFalse(service_stop_event.is_set())

            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
