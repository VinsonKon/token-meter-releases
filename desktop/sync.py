#!/usr/bin/env python3
"""Read official account usage plus local task state and sync it over BLE."""

from __future__ import annotations

import argparse
import asyncio
import copy
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import Request, urlopen

import codex_account
import model_registry
import source_registry
import update_manager
import skin_manager


DEVICE_NAME = "Codex Meter"
DEVICE_NAME_PREFIX = DEVICE_NAME + "-"
SERVICE_UUID = "7f520001-1b15-4f9f-8bd8-29dbe9e681a1"
DATA_CHARACTERISTIC_UUID = "7f520002-1b15-4f9f-8bd8-29dbe9e681a1"
ACK_CHARACTERISTIC_UUID = "7f520003-1b15-4f9f-8bd8-29dbe9e681a1"

PROTOCOL_VERSION = 1
MAX_JSON_SIZE = 512
ACK_SIZE = 5

# Same precedence used by token-monitor v0.24.0: manual override, fresh online
# rate, cached rate, then a built-in offline floor.
DEFAULT_USD_CNY = 6.8
FX_SOURCES = (
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json",
    "https://latest.currency-api.pages.dev/v1/currencies/usd.json",
)

CODEX_TASK_ORPHAN_TIMEOUT_SECONDS = 30.0 * 60.0
CODEX_TASK_SESSION_SCAN_LIMIT = 16
CODEX_SESSION_DISCOVERY_INTERVAL_SECONDS = 2.0
CODEX_DAILY_SESSION_SCAN_INTERVAL_SECONDS = 30.0
CODEX_COLD_SCAN_LIMIT = 32
TOKEN_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

CODEX_TASK_START_EVENTS = frozenset((
    "task_started",
    "turn_started",
))
CODEX_TASK_END_EVENTS = frozenset((
    "task_complete",
    "task_completed",
    "task_cancelled",
    "task_canceled",
    "task_failed",
    "turn_complete",
    "turn_completed",
    "turn_aborted",
    "turn_cancelled",
    "turn_canceled",
    "turn_failed",
))
CODEX_TASK_ABORT_EVENTS = frozenset((
    "task_cancelled",
    "task_canceled",
    "task_failed",
    "turn_aborted",
    "turn_cancelled",
    "turn_canceled",
    "turn_failed",
))
CODEX_TOOL_CALL_EVENTS = frozenset((
    "function_call",
    "custom_tool_call",
    "computer_call",
    "image_generation_call",
    "mcp_call",
))
CODEX_TOOL_TERMINAL_STATUSES = frozenset((
    "cancelled",
    "canceled",
    "failed",
))

TEST_STATE_NAMES = (
    "DEFAULT", "THINKING", "NEED APPROVAL", "LIMIT REACHED", "DATA STALE",
    "WAITING FOR SYNC", "CHARGING", "LOW BATTERY",
    "UPDATE AVAILABLE", "DOWNLOADING", "INSTALLING",
    "VERIFYING", "RESTARTING", "UPDATE FAILED", "ROLLED BACK",
    "SINGLE QUOTA",
)
TEST_MODE_INITIAL_STATE = 1

_sequence_lock = threading.Lock()
_last_sequence = 0


class MultipleDevicesFoundError(RuntimeError):
    def __init__(self, devices: list[dict[str, str]]) -> None:
        self.devices = devices
        labels = ", ".join(device["name"] for device in devices)
        super().__init__(
            f"Multiple Codex Meters found ({labels}); select one from the tray Device menu"
        )


def device_address_suffix(address: str) -> str:
    compact = "".join(character for character in address if character.isalnum())
    return compact[-4:].upper() if compact else "????"


def device_display_name(name: str | None, address: str) -> str:
    advertised = (name or "").strip()
    if advertised.startswith(DEVICE_NAME_PREFIX):
        return advertised
    return f"{DEVICE_NAME_PREFIX}{device_address_suffix(address)}"


def is_meter_advertisement(name: str | None,
                           service_uuids: list[str] | None) -> bool:
    advertised = (name or "").strip()
    normalized_services = [uuid.lower() for uuid in (service_uuids or [])]
    return (
        advertised == DEVICE_NAME
        or advertised.startswith(DEVICE_NAME_PREFIX)
        or SERVICE_UUID in normalized_services
    )


def state_directory() -> Path:
    override = os.environ.get("CODEX_METER_HOME")
    if override:
        path = Path(override).expanduser()
    elif getattr(sys, "frozen", False):
        path = Path(os.environ.get("APPDATA", str(Path.home()))) / "CodexMeter"
    else:
        path = Path(__file__).resolve().parent
    path.mkdir(parents=True, exist_ok=True)
    return path


def _emit(message: str, callback: Callable[[str], None] | None = None,
          *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    # PyInstaller --windowed intentionally sets stdout/stderr to None.
    if stream is not None:
        print(message, file=stream, flush=True)
    if callback:
        callback(message)


def _write_text_best_effort(path: Path, value: str) -> None:
    try:
        path.write_text(value, encoding="utf-8")
    except OSError:
        pass


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _bounded_u8(value: Any) -> int:
    return max(0, min(100, round(float(value or 0))))


def _device_used_percent(value: Any) -> int:
    """Quantize usage without losing the strict below-10% alert boundary."""
    bounded = _bounded_u8(value)
    try:
        raw = float(value or 0)
    except (TypeError, ValueError, OverflowError):
        return bounded
    # Python's nearest-even rounding maps 90.1-90.5 to 90. That would set the
    # global limit flag while making firmware see both windows at >=10%
    # remaining, so it could highlight the wrong fallback card. Preserve the
    # alert side of this one boundary in the compact integer wire value.
    if raw > 90.0 and bounded == 90:
        return 91
    return bounded


def _bounded_u16(value: Any) -> int:
    return max(0, min(0xFFFF, _safe_int(value)))


def _bounded_u32(value: Any) -> int:
    return max(0, min(0xFFFFFFFF, _safe_int(value)))


def _bounded_u64(value: Any) -> int:
    return max(0, min(0xFFFFFFFFFFFFFFFF, _safe_int(value)))


def _bounded_text(value: Any, max_bytes: int) -> str:
    raw = str(value or "").encode("utf-8")[:max_bytes]
    return raw.decode("utf-8", errors="ignore")


def estimate_cost_usd(snapshot: dict[str, Any]) -> float | None:
    """Estimate today's standard API-equivalent token cost in USD."""
    # account/usage/read exposes a lifetime diagnostic total, but not its
    # input/cache/output split. Keep API-equivalent daily cost based solely on
    # local session deltas; never price the lifetime account total as one day.
    by_model = snapshot.get("cost_tokens_by_model")
    if isinstance(by_model, dict):
        total_cost = 0.0
        saw_usage = False
        saw_priced_usage = False
        for model, raw_totals in by_model.items():
            if not isinstance(raw_totals, dict):
                continue
            input_tokens = _bounded_u64(raw_totals.get("input_tokens"))
            output_tokens = _bounded_u64(raw_totals.get("output_tokens"))
            total_tokens = _bounded_u64(raw_totals.get("total_tokens"))
            if input_tokens == 0 and output_tokens == 0 and total_tokens == 0:
                continue
            saw_usage = True
            pricing = model_registry.pricing_for_model(model)
            if pricing is None:
                # Unknown/internal future model ids are omitted from the RMB
                # estimate without invalidating the known-model subtotal.
                continue
            saw_priced_usage = True
            cached_tokens = min(
                input_tokens,
                _bounded_u64(raw_totals.get("cached_input_tokens")),
            )
            uncached_tokens = input_tokens - cached_tokens
            input_rate, cached_rate, output_rate = pricing
            total_cost += (
                uncached_tokens * input_rate
                + cached_tokens * cached_rate
                + output_tokens * output_rate
            ) / 1_000_000
        if saw_priced_usage:
            return total_cost
        if saw_usage:
            return None
        return 0.0 if snapshot.get("token_scope") == "local_daily" else None

    pricing = model_registry.pricing_for_model(snapshot.get("model"))
    totals = (
        snapshot.get("cost_tokens") or {}
        if "cost_tokens" in snapshot
        else snapshot.get("tokens") or {}
    )
    if not totals:
        return None
    input_tokens = _bounded_u64(totals.get("input_tokens"))
    cached_tokens = min(input_tokens, _bounded_u64(totals.get("cached_input_tokens")))
    uncached_tokens = input_tokens - cached_tokens
    output_tokens = _bounded_u64(totals.get("output_tokens"))
    if input_tokens == 0 and output_tokens == 0:
        return 0.0 if snapshot.get("token_scope") == "local_daily" else None
    if pricing is None:
        return None
    input_rate, cached_rate, output_rate = pricing
    return (
        uncached_tokens * input_rate
        + cached_tokens * cached_rate
        + output_tokens * output_rate
    ) / 1_000_000


def _valid_positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number > 0 else None


def _read_fx_cache(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _cached_cny_rate(cache: dict[str, Any] | None) -> float | None:
    return _valid_positive_number((cache or {}).get("usd_cny"))


def resolve_usd_cny(
    cache_path: Path, manual_rate: float | None, allow_live: bool = True
) -> tuple[float, str]:
    manual = _valid_positive_number(manual_rate)
    if manual is not None:
        return manual, "manual"

    cache = _read_fx_cache(cache_path)
    cached_rate = _cached_cny_rate(cache)
    fetched_at = _valid_positive_number((cache or {}).get("fetched_at")) or 0
    cache_fresh = cached_rate is not None and time.time() - fetched_at < 86400
    if cache_fresh:
        return cached_rate, "cache"

    if allow_live:
        for url in FX_SOURCES:
            try:
                request = Request(url, headers={"User-Agent": "Codex-Meter/1.0"})
                with urlopen(request, timeout=4.0) as response:
                    data = json.load(response)
                rate = _valid_positive_number((data.get("usd") or {}).get("cny"))
                if rate is None:
                    continue
                payload = {
                    "usd_cny": rate,
                    "date": data.get("date"),
                    "source": url,
                    "fetched_at": time.time(),
                }
                try:
                    cache_path.write_text(json.dumps(payload), encoding="utf-8")
                except OSError:
                    pass
                return rate, "live"
            except (OSError, URLError, TimeoutError, ValueError, json.JSONDecodeError):
                continue

    if cached_rate is not None:
        return cached_rate, "stale-cache"
    return DEFAULT_USD_CNY, "fallback"


def cost_cny_for_snapshot(
    snapshot: dict[str, Any], manual_cost: float | None, usd_cny: float
) -> tuple[float | None, str]:
    if manual_cost is not None:
        return max(0.0, manual_cost), "manual-cost"
    cost_usd = estimate_cost_usd(snapshot)
    if cost_usd is None:
        return None, "unavailable"
    return cost_usd * usd_cny, "api-equivalent"


def _recent_jsonl_files(folders: tuple[Path, ...], limit: int) -> list[Path]:
    """Return recent JSONL files for cold-start/fallback discovery only."""
    files: list[tuple[int, Path]] = []
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.rglob("*.jsonl"):
            try:
                files.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
    files.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in files[:limit]]


def _empty_token_totals() -> dict[str, int]:
    return {key: 0 for key in TOKEN_KEYS}


@dataclass
class _CodexSessionState:
    path: Path
    offset: int = 0
    partial: bytes = b""
    size: int = 0
    mtime_ns: int = 0
    trackable: bool | None = None
    model: str = ""
    model_epoch: float = float("-inf")
    task_active: bool | None = None
    task_epoch: float = float("-inf")
    open_calls: set[str] = field(default_factory=set)
    image_calls: set[str] = field(default_factory=set)
    approval_calls: set[str] = field(default_factory=set)
    terminal_pending: bool = False
    seen_token_events: set[bytes] = field(default_factory=set)
    previous_tokens: dict[str, int] = field(default_factory=_empty_token_totals)
    daily_tokens: dict[str, int] = field(default_factory=_empty_token_totals)
    daily_tokens_by_model: dict[str, dict[str, int]] = field(
        default_factory=dict
    )
    daily_activity: float = 0.0


@dataclass
class _CodexHomeCache:
    home: Path
    day_start: float
    states: dict[Path, _CodexSessionState] = field(default_factory=dict)
    candidates: list[Path] = field(default_factory=list)
    dirty_paths: set[Path] = field(default_factory=set)
    last_discovery: float = 0.0
    cold_scan_complete: bool = False
    last_daily_scan: float = 0.0
    seen_token_events: set[bytes] = field(default_factory=set)


_codex_cache_lock = threading.RLock()
_codex_caches: dict[str, _CodexHomeCache] = {}
_claude_candidate_lock = threading.Lock()
_claude_candidate_cache: dict[str, tuple[float, list[Path]]] = {}


def _local_day_start() -> float:
    local_now = datetime.now().astimezone()
    return local_now.replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()


def _codex_cache(codex_home: Path) -> _CodexHomeCache:
    resolved = codex_home.expanduser().resolve()
    key = os.path.normcase(str(resolved))
    cache = _codex_caches.get(key)
    day_start = _local_day_start()
    if cache is None or cache.day_start != day_start:
        cache = _CodexHomeCache(resolved, day_start)
        _codex_caches[key] = cache
    return cache


def _session_day_directories(sessions: Path) -> tuple[Path, Path]:
    local_now = datetime.now().astimezone()
    days = (local_now, local_now - timedelta(days=1))
    return tuple(
        sessions / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")
        for day in days
    )


def _discover_codex_candidates(
    cache: _CodexHomeCache, *, force: bool = False
) -> list[Path]:
    """Discover current logs while keeping the frequent scan inexpensive.

    Codex stores normal sessions under sessions/YYYY/MM/DD. The hot path only
    lists today, yesterday (for a task crossing midnight), and root-level
    JSONL files used by older clients. A metadata-only recursive scan runs at
    startup and periodically so a resumed session in an older creation-date
    directory cannot be hidden by unrelated files in the hot directories.
    archived_sessions is deliberately never consulted.
    """
    now = time.monotonic()
    if (
        not force
        and cache.candidates
        and now - cache.last_discovery
        < CODEX_SESSION_DISCOVERY_INTERVAL_SECONDS
    ):
        return cache.candidates

    sessions = cache.home / "sessions"
    found: set[Path] = set()
    directories = (sessions, *_session_day_directories(sessions))
    for directory in directories:
        if not directory.exists():
            continue
        try:
            found.update(path for path in directory.glob("*.jsonl") if path.is_file())
        except OSError:
            continue

    # A watcher event is authoritative even when the changed file lives below
    # an old creation-date directory. Keep the dirty marker until the file has
    # actually been consumed by _update_codex_session().
    found.update(path for path in cache.dirty_paths if path.is_file())

    # Retain a known running or today-modified session even when it crossed a
    # date boundary. Re-stat the small bounded state set instead of trusting
    # the cached mtime: a previously idle task can resume after it was removed
    # from candidates, and a filesystem watcher event can occasionally be
    # coalesced or missed.
    for path, state in cache.states.items():
        if state.trackable is False:
            continue
        try:
            observed_mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        if state.task_active or (
            observed_mtime_ns / 1_000_000_000 >= cache.day_start
        ):
            found.add(path)

    if (
        not cache.cold_scan_complete
        or now - cache.last_daily_scan
        >= CODEX_DAILY_SESSION_SCAN_INTERVAL_SECONDS
    ):
        if sessions.exists():
            try:
                for path in sessions.rglob("*.jsonl"):
                    try:
                        if path.stat().st_mtime >= cache.day_start:
                            found.add(path)
                    except OSError:
                        continue
            except OSError:
                pass
        cache.cold_scan_complete = True
        cache.last_daily_scan = now

    if not found and not cache.states:
        found.update(
            _recent_jsonl_files((sessions,), CODEX_COLD_SCAN_LIMIT)
        )

    metadata: list[tuple[int, Path]] = []
    for path in found:
        try:
            metadata.append((path.stat().st_mtime_ns, path))
        except OSError:
            continue
    metadata.sort(key=lambda item: item[0], reverse=True)
    cache.candidates = [path for _, path in metadata]
    cache.last_discovery = now
    cache.dirty_paths.intersection_update(cache.candidates)
    return cache.candidates


def _reset_session_state(state: _CodexSessionState) -> None:
    path = state.path
    state.__dict__.update(_CodexSessionState(path).__dict__)


def _session_meta_trackable(event: dict[str, Any]) -> bool | None:
    """Classify the first session_meta without inspecting arbitrary text."""
    if event.get("type") != "session_meta":
        return None
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        return True
    thread_source = str(payload.get("thread_source") or "").strip().lower()
    if thread_source == "user":
        return True
    if thread_source in {"subagent", "guardian", "internal"}:
        return False
    source = payload.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return False
    # Legacy CLI/Desktop metadata did not always expose thread_source.
    return True


def _clear_session_aggregates(state: _CodexSessionState) -> None:
    state.model = ""
    state.model_epoch = float("-inf")
    state.task_active = None
    state.task_epoch = float("-inf")
    state.open_calls.clear()
    state.image_calls.clear()
    state.approval_calls.clear()
    state.terminal_pending = False
    state.seen_token_events.clear()
    state.previous_tokens = _empty_token_totals()
    state.daily_tokens = _empty_token_totals()
    state.daily_tokens_by_model.clear()
    state.daily_activity = 0.0


def _apply_task_event(state: _CodexSessionState, event: dict[str, Any]) -> None:
    payload = event.get("payload") or {}
    payload_type = payload.get("type")
    epoch = _event_epoch(event)

    if event.get("type") == "event_msg":
        if payload_type in CODEX_TASK_START_EVENTS:
            state.task_active = True
            state.open_calls.clear()
            state.image_calls.clear()
            state.approval_calls.clear()
            state.terminal_pending = False
            if epoch is not None:
                state.task_epoch = epoch
            return

        is_final_answer = (
            payload_type == "agent_message"
            and payload.get("phase") == "final_answer"
        )
        if payload_type in CODEX_TASK_END_EVENTS or is_final_answer:
            if payload_type in CODEX_TASK_ABORT_EVENTS:
                state.open_calls.clear()
                state.image_calls.clear()
                state.approval_calls.clear()
                state.terminal_pending = False
                state.task_active = False
                if epoch is not None:
                    state.task_epoch = epoch
                return
            state.terminal_pending = True
            state.task_active = bool(state.open_calls)
            if epoch is not None:
                state.task_epoch = epoch
            return

    if event.get("type") != "response_item":
        return

    call_id = _codex_tool_call_id(event)
    if not call_id:
        return

    if payload_type in CODEX_TOOL_CALL_EVENTS:
        status = str(payload.get("status") or "").lower()
        is_image_call = _codex_is_image_tool(payload)
        if is_image_call:
            state.image_calls.add(call_id)
        image_done = (
            payload_type == "image_generation_call"
            and status == "completed"
        )
        if image_done or status in CODEX_TOOL_TERMINAL_STATUSES:
            state.open_calls.discard(call_id)
            state.image_calls.discard(call_id)
            state.approval_calls.discard(call_id)
        else:
            state.open_calls.add(call_id)
            if _codex_call_requires_approval(payload):
                state.approval_calls.add(call_id)
            if state.task_active is None:
                state.task_active = True
        if state.terminal_pending or (image_done and not state.open_calls):
            state.task_active = bool(state.open_calls)
        if epoch is not None:
            state.task_epoch = epoch
        return

    if isinstance(payload_type, str) and payload_type.endswith("_output"):
        image_finished = call_id in state.image_calls
        state.open_calls.discard(call_id)
        state.image_calls.discard(call_id)
        state.approval_calls.discard(call_id)
        if state.terminal_pending or (image_finished and not state.open_calls):
            state.task_active = bool(state.open_calls)
        if epoch is not None:
            state.task_epoch = epoch


def _apply_codex_event(
    state: _CodexSessionState,
    event: dict[str, Any],
    day_start: float,
    now: float,
    seen_token_events: set[bytes],
) -> None:
    session_trackable = _session_meta_trackable(event)
    if session_trackable is not None:
        # Forked subagent logs include copied parent session_meta records after
        # their own metadata. Only the first record identifies this file.
        if state.trackable is None:
            state.trackable = session_trackable
            if not session_trackable:
                _clear_session_aggregates(state)
        return
    if state.trackable is False:
        return

    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    epoch = _event_epoch(event)

    _apply_task_event(state, event)

    if event.get("type") == "turn_context":
        model = str(payload.get("model") or "")
        if model:
            state.model = model
            if epoch is not None:
                state.model_epoch = epoch

    if payload.get("type") != "token_count" or epoch is None:
        return
    # Session total_token_usage supplies cumulative counters. Their daily
    # differences feed the all-model dashboard total and the internal
    # per-model RMB breakdown; they are never an account quota or balance.
    totals = ((payload.get("info") or {}).get("total_token_usage") or {})
    if not totals:
        return
    values = {key: _bounded_u64(totals.get(key)) for key in TOKEN_KEYS}
    encoded = json.dumps(
        event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    fingerprint = hashlib.blake2b(encoded, digest_size=16).digest()
    if fingerprint in state.seen_token_events:
        # An old cumulative record can be replayed after a newer one. This
        # applies across midnight too, so never move the baseline backwards.
        return
    state.seen_token_events.add(fingerprint)
    duplicate = fingerprint in seen_token_events
    seen_token_events.add(fingerprint)
    if duplicate:
        # A copied record in a second file establishes that file's local
        # cumulative baseline but contributes no additional usage.
        state.previous_tokens = values
        return
    if day_start <= epoch <= now:
        deltas = {
            key: (
                values[key] - state.previous_tokens[key]
                if values[key] >= state.previous_tokens[key]
                else values[key]
            )
            for key in TOKEN_KEYS
        }
        for key, delta in deltas.items():
            state.daily_tokens[key] += delta
        model_id = model_registry.canonical_model_id(state.model)
        model_totals = state.daily_tokens_by_model.setdefault(
            model_id, _empty_token_totals()
        )
        for key, delta in deltas.items():
            model_totals[key] += delta
        state.daily_activity = max(state.daily_activity, epoch)
    state.previous_tokens = values


def _update_codex_session(
    cache: _CodexHomeCache, path: Path
) -> _CodexSessionState | None:
    try:
        stat = path.stat()
    except OSError:
        cache.states.pop(path, None)
        return None

    state = cache.states.setdefault(path, _CodexSessionState(path))
    if stat.st_size < state.offset:
        _reset_session_state(state)
    if (
        stat.st_size == state.size
        and stat.st_mtime_ns == state.mtime_ns
        and path not in cache.dirty_paths
    ):
        return state

    try:
        with path.open("rb") as stream:
            stream.seek(state.offset)
            data = stream.read()
            state.offset = stream.tell()
    except OSError:
        return state

    state.size = stat.st_size
    state.mtime_ns = stat.st_mtime_ns
    cache.dirty_paths.discard(path)
    if not data:
        return state

    chunks = (state.partial + data).split(b"\n")
    state.partial = chunks.pop()
    if state.partial:
        try:
            json.loads(state.partial)
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass
        else:
            chunks.append(state.partial)
            state.partial = b""

    now = time.time()
    for raw in chunks:
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            continue
        if isinstance(event, dict):
            _apply_codex_event(
                state, event, cache.day_start, now,
                cache.seen_token_events,
            )
    return state


def _refresh_codex_cache(codex_home: Path) -> _CodexHomeCache:
    with _codex_cache_lock:
        cache = _codex_cache(codex_home)
        candidates = _discover_codex_candidates(
            cache, force=bool(cache.dirty_paths)
        )
        truncated = False
        for path in candidates:
            state = cache.states.get(path)
            if state is None:
                continue
            try:
                truncated = path.stat().st_size < state.offset
            except OSError:
                continue
            if truncated:
                break
        if truncated:
            # Event fingerprints are global to the home cache. Rebuilding all
            # session state together prevents a truncated/replaced JSONL file
            # from having its valid replay suppressed by stale fingerprints.
            cache.states.clear()
            cache.candidates.clear()
            cache.dirty_paths.clear()
            cache.seen_token_events.clear()
            cache.last_discovery = 0.0
            cache.cold_scan_complete = False
            cache.last_daily_scan = 0.0
            candidates = _discover_codex_candidates(cache, force=True)
        for path in candidates:
            _update_codex_session(cache, path)
        cache.candidates = [
            path for path in cache.candidates
            if (
                cache.states.get(path) is None
                or cache.states[path].trackable is not False
            )
        ]
        return cache


def _mark_codex_path_dirty(codex_home: Path, path: Path) -> None:
    try:
        resolved = path.resolve()
        sessions = (codex_home / "sessions").resolve()
        resolved.relative_to(sessions)
    except (OSError, ValueError):
        return
    with _codex_cache_lock:
        cache = _codex_cache(codex_home)
        cache.dirty_paths.add(resolved)
        cache.last_discovery = 0.0


def _candidate_files(codex_home: Path) -> list[Path]:
    return list(_refresh_codex_cache(codex_home).candidates)


def _tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    """Read complete lines near EOF without loading a potentially huge session."""
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, os.SEEK_END)
            start = max(0, size - max_bytes)
            stream.seek(start)
            data = stream.read()
    except OSError:
        return []
    if start > 0:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return data.decode("utf-8", errors="replace").splitlines()


def _codex_task_marker(event: dict[str, Any]) -> bool | None:
    if event.get("type") != "event_msg":
        return None
    payload = event.get("payload") or {}
    payload_type = payload.get("type")
    if payload_type in CODEX_TASK_START_EVENTS:
        return True
    if payload_type in CODEX_TASK_END_EVENTS:
        return False
    # Codex CLI/Desktop can emit the final answer even when task_complete is
    # delayed or omitted. Commentary messages are deliberately not terminal.
    if (
        payload_type == "agent_message"
        and payload.get("phase") == "final_answer"
    ):
        return False
    return None


def _codex_tool_call_id(event: dict[str, Any]) -> str:
    payload = event.get("payload") or {}
    value = (
        payload.get("call_id")
        or payload.get("id")
        or event.get("call_id")
        or event.get("id")
    )
    return str(value) if value else ""


def _codex_call_requires_approval(payload: dict[str, Any]) -> bool:
    """Recognize an elevated tool call without searching arbitrary log text."""
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(arguments, dict):
        return _codex_exec_input_requires_approval(payload)
    return arguments.get("sandbox_permissions") == "require_escalated"


def _codex_exec_input_requires_approval(payload: dict[str, Any]) -> bool:
    """Read scoped command options from a modern exec call."""
    if payload.get("type") != "custom_tool_call":
        return False
    if payload.get("name") != "exec":
        return False
    source = payload.get("input")
    if not isinstance(source, str):
        return False

    tokens = _javascript_tokens(source)
    index = 0
    while index + 3 < len(tokens):
        if (
            tokens[index:index + 2] != [
                ("name", "tools"),
                ("punct", "."),
            ]
            or tokens[index + 2] not in (
                ("name", "shell_command"),
                ("name", "exec_command"),
            )
            or tokens[index + 3] != ("punct", "(")
        ):
            index += 1
            continue
        if _javascript_call_has_escalated_permission(tokens, index + 4):
            return True
        index += 4
    return False


def _javascript_call_has_escalated_permission(
    tokens: list[tuple[str, str]],
    index: int,
) -> bool:
    """Inspect only top-level properties of a command argument object."""
    if index >= len(tokens) or tokens[index] != ("punct", "{"):
        return False
    depth = 1
    index += 1
    while index < len(tokens) and depth:
        token = tokens[index]
        if token == ("punct", "{"):
            depth += 1
        elif token == ("punct", "}"):
            depth -= 1
        elif (
            depth == 1
            and token in (
                ("name", "sandbox_permissions"),
                ("string", "sandbox_permissions"),
            )
            and index + 2 < len(tokens)
            and tokens[index + 1] == ("punct", ":")
            and tokens[index + 2] == ("string", "require_escalated")
        ):
            return True
        index += 1
    return False


def _javascript_tokens(source: str) -> list[tuple[str, str]]:
    """Tokenize enough JavaScript to inspect an object without executing it."""
    tokens: list[tuple[str, str]] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            newline = source.find("\n", index + 2)
            index = length if newline < 0 else newline + 1
            continue
        if source.startswith("/*", index):
            closing = source.find("*/", index + 2)
            index = length if closing < 0 else closing + 2
            continue
        if char in ("'", '"', "`"):
            quote = char
            index += 1
            value: list[str] = []
            while index < length:
                char = source[index]
                if char == "\\" and index + 1 < length:
                    value.append(source[index + 1])
                    index += 2
                    continue
                if char == quote:
                    index += 1
                    break
                value.append(char)
                index += 1
            tokens.append(("string", "".join(value)))
            continue
        if char.isalpha() or char in ("_", "$"):
            end = index + 1
            while end < length and (
                source[end].isalnum() or source[end] in ("_", "$")
            ):
                end += 1
            tokens.append(("name", source[index:end]))
            index = end
            continue
        if char in ".(){}[]:,":
            tokens.append(("punct", char))
        index += 1
    return tokens


def _codex_is_image_tool(payload: dict[str, Any]) -> bool:
    if payload.get("type") == "image_generation_call":
        return True
    compact_name = "".join(
        character for character in str(payload.get("name") or "").lower()
        if character.isalnum()
    )
    return "imagegen" in compact_name


def _codex_session_runtime_state(
    lines: list[str],
) -> tuple[bool | None, bool, float]:
    """Reduce one session while keeping asynchronous tool calls in-flight.

    Image generation can continue after Codex has emitted a final-answer or
    even a task-complete marker. Tool calls and their outputs are separate
    response items, so a terminal lifecycle marker only becomes effective
    after every call that started in the turn has produced an output.
    """
    state = _CodexSessionState(Path())
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict):
            _apply_task_event(state, event)
    return state.task_active, bool(state.approval_calls), state.task_epoch


def _codex_session_task_state(
    lines: list[str],
) -> tuple[bool | None, float]:
    """Compatibility wrapper returning only the task lifecycle state."""
    task_active, _, task_epoch = _codex_session_runtime_state(lines)
    return task_active, task_epoch


def read_codex_runtime_state(
    codex_home: Path,
) -> tuple[bool | None, bool, float]:
    """Return aggregate task and approval state for recent Codex sessions.

    Codex sessions can run concurrently. Looking only at the globally newest
    lifecycle event makes a completed task hide another session that is still
    running, so resolve each session and OR the resulting active states. A
    final answer closes CLI turns that omit task_complete, except while an
    asynchronous tool (notably image generation) is still in flight.
    Abandoned sessions cannot remain active forever. Archived sessions are not
    part of the realtime path.
    """
    cache = _refresh_codex_cache(codex_home)
    session_states: list[bool] = []
    needs_approval = False
    newest_epoch = float("-inf")
    selected = [
        path for path in cache.candidates
        if (
            cache.states.get(path) is None
            or cache.states[path].trackable is not False
        )
    ][:CODEX_TASK_SESSION_SCAN_LIMIT]
    selected.extend(
        path for path, state in cache.states.items()
        if (
            state.trackable is not False
            and (state.task_active or state.approval_calls)
            and path not in selected
        )
    )
    for path in selected:
        state = cache.states.get(path)
        if state is None:
            continue
        session_active = state.task_active
        session_needs_approval = bool(state.approval_calls)
        session_epoch = state.task_epoch
        if session_active is not None:
            if session_active:
                try:
                    file_epoch = path.stat().st_mtime
                except OSError:
                    file_epoch = session_epoch
                last_activity_epoch = max(session_epoch, file_epoch)
                abandoned = (
                    last_activity_epoch != float("-inf")
                    and time.time() - last_activity_epoch
                    > CODEX_TASK_ORPHAN_TIMEOUT_SECONDS
                )
                if abandoned:
                    state.task_active = False
                    state.open_calls.clear()
                    state.image_calls.clear()
                    state.approval_calls.clear()
                    state.terminal_pending = False
                    session_active = False
                    session_needs_approval = False
            session_states.append(session_active)
            needs_approval = needs_approval or session_needs_approval
            newest_epoch = max(newest_epoch, session_epoch)
    if not session_states:
        return None, False, newest_epoch
    return any(session_states), needs_approval, newest_epoch


def read_codex_task_state(codex_home: Path) -> tuple[bool | None, float]:
    """Compatibility wrapper returning only aggregate task activity."""
    task_active, _, task_epoch = read_codex_runtime_state(codex_home)
    return task_active, task_epoch


def _event_epoch(event: dict[str, Any]) -> float | None:
    value = event.get("timestamp")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _daily_cost_token_totals(
    codex_home: Path,
) -> tuple[dict[str, int], dict[str, dict[str, int]], float]:
    """Return all-model daily totals plus per-model pricing counters."""
    cache = _refresh_codex_cache(codex_home)
    result = _empty_token_totals()
    by_model: dict[str, dict[str, int]] = {}
    latest_activity = 0.0
    for state in cache.states.values():
        if state.trackable is False:
            continue
        for key in TOKEN_KEYS:
            result[key] += state.daily_tokens[key]
        for model_id, model_values in state.daily_tokens_by_model.items():
            model_result = by_model.setdefault(model_id, _empty_token_totals())
            for key in TOKEN_KEYS:
                model_result[key] += model_values[key]
        latest_activity = max(latest_activity, state.daily_activity)
    return result, by_model, latest_activity


def _codex_session_metadata(
    cache: _CodexHomeCache,
) -> tuple[str, str, float]:
    """Return the most recent local model/source without using session quota."""
    model = ""
    source = ""
    latest_epoch = float("-inf")
    for path in cache.candidates:
        state = cache.states.get(path)
        if state is None or state.trackable is False:
            continue
        try:
            file_epoch = path.stat().st_mtime
        except OSError:
            file_epoch = 0.0
        activity_epoch = max(
            file_epoch,
            state.model_epoch,
            state.task_epoch,
            state.daily_activity,
        )
        if activity_epoch > latest_epoch:
            latest_epoch = activity_epoch
            source = str(path)
        if not model and state.model:
            model = state.model
    return model, source, latest_epoch


def _read_codex_usage(
    codex_home: Path, account_cache: Path | None = None, *,
    force_account: bool = False,
) -> dict[str, Any]:
    """Combine official account data with local task/model activity only."""
    cache = _refresh_codex_cache(codex_home)
    account_cache = account_cache or (
        state_directory() / ".codex-account.json"
    )
    snapshot = codex_account.read_account_snapshot(
        account_cache, codex_home, force=force_account
    )
    detected_model, session_source, newest_epoch = _codex_session_metadata(
        cache
    )
    snapshot["model"] = model_registry.canonical_model_id(detected_model)
    snapshot["detected_model"] = detected_model
    snapshot["provider"] = "openai"
    snapshot["source_id"] = source_registry.CODEX_SOURCE
    snapshot["latest_epoch"] = newest_epoch
    snapshot["source"] = session_source or snapshot.get(
        "account_source", "Codex App Server"
    )
    # The device displays official quota windows but replaces the official
    # lifetime usage total with locally parsed daily totals below. A prolonged
    # usage-summary outage must therefore not mask otherwise current device
    # data with DATA STALE.
    snapshot["device_data_stale"] = bool(
        snapshot.get(
            "rate_limit_data_stale",
            snapshot.get("account_data_stale", False),
        )
    )
    daily_totals, daily_by_model, latest_activity = (
        _daily_cost_token_totals(codex_home)
    )
    # The dashboard total is today's aggregate across every eligible model.
    # Per-model input/cache/output counters remain internal and exist only so
    # each known model can be priced correctly. Official account usage is
    # retained for diagnostics, but guardian/subagent usage cannot be removed
    # from that server-side lifetime aggregate and is therefore not displayed.
    snapshot["account_tokens"] = copy.deepcopy(snapshot.get("tokens") or {})
    snapshot["tokens"] = _empty_token_totals()
    snapshot["tokens"]["total_tokens"] = daily_totals["total_tokens"]
    snapshot["token_scope"] = "local_daily"
    snapshot["cost_tokens"] = daily_totals
    snapshot["cost_tokens_by_model"] = daily_by_model
    snapshot["activity_epoch"] = latest_activity
    task_active, needs_approval, task_state_epoch = read_codex_runtime_state(
        codex_home
    )
    snapshot["task_active"] = task_active
    snapshot["needs_approval"] = needs_approval
    snapshot["task_state_epoch"] = task_state_epoch
    _refresh_activity(snapshot)
    return snapshot


def _claude_candidate_files(claude_home: Path) -> list[Path]:
    projects = claude_home / "projects"
    key = os.path.normcase(str(projects.expanduser().resolve()))
    now = time.monotonic()
    with _claude_candidate_lock:
        cached = _claude_candidate_cache.get(key)
        if (
            cached
            and now - cached[0] < CODEX_SESSION_DISCOVERY_INTERVAL_SECONDS
        ):
            return list(cached[1])
        files = _recent_jsonl_files((projects,), 250)
        _claude_candidate_cache[key] = (now, files)
        return list(files)


def _mark_claude_candidates_dirty(claude_home: Path) -> None:
    key = os.path.normcase(
        str((claude_home / "projects").expanduser().resolve())
    )
    with _claude_candidate_lock:
        _claude_candidate_cache.pop(key, None)


def _read_claude_code_usage(claude_home: Path) -> dict[str, Any]:
    """Read today's Claude Code assistant-message usage from local JSONL logs."""
    now = time.time()
    local_now = datetime.now().astimezone()
    day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    messages: dict[str, dict[str, Any]] = {}
    latest_epoch = float("-inf")
    latest_model = ""
    latest_source = ""

    for path in _claude_candidate_files(claude_home):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line_number, line in enumerate(lines):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if event.get("type") != "assistant":
                continue
            epoch = _event_epoch(event)
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            if epoch is None or not isinstance(message, dict) or not usage:
                continue
            model = str(message.get("model") or "")
            if epoch > latest_epoch:
                latest_epoch = epoch
                latest_model = model
                latest_source = str(path)
            if epoch < day_start or epoch > now:
                continue

            # Claude may append multiple records for one streamed assistant
            # message. Keep the largest counters per message instead of
            # double-counting tool-use updates.
            message_id = str(message.get("id") or f"{path}:{line_number}")
            previous = messages.get(message_id, {})
            messages[message_id] = {
                "model": model or str(previous.get("model") or ""),
                "input_tokens": max(
                    _bounded_u64(previous.get("input_tokens")),
                    _bounded_u64(usage.get("input_tokens"))
                    + _bounded_u64(usage.get("cache_creation_input_tokens"))
                    + _bounded_u64(usage.get("cache_read_input_tokens")),
                ),
                "cached_input_tokens": max(
                    _bounded_u64(previous.get("cached_input_tokens")),
                    _bounded_u64(usage.get("cache_read_input_tokens")),
                ),
                "output_tokens": max(
                    _bounded_u64(previous.get("output_tokens")),
                    _bounded_u64(usage.get("output_tokens")),
                ),
            }

    if latest_epoch == float("-inf"):
        raise FileNotFoundError(
            f"No Claude Code usage event found below {claude_home}. "
            "Run at least one Claude Code request, then try again."
        )

    totals = {
        "input_tokens": sum(item["input_tokens"] for item in messages.values()),
        "cached_input_tokens": sum(
            item["cached_input_tokens"] for item in messages.values()
        ),
        "output_tokens": sum(item["output_tokens"] for item in messages.values()),
        "reasoning_output_tokens": 0,
    }
    totals["total_tokens"] = totals["input_tokens"] + totals["output_tokens"]
    by_model: dict[str, dict[str, int]] = {}
    for item in messages.values():
        model_id = model_registry.canonical_model_id(item.get("model"))
        model_totals = by_model.setdefault(model_id, _empty_token_totals())
        model_totals["input_tokens"] += item["input_tokens"]
        model_totals["cached_input_tokens"] += item["cached_input_tokens"]
        model_totals["output_tokens"] += item["output_tokens"]
        model_totals["total_tokens"] += (
            item["input_tokens"] + item["output_tokens"]
        )
    return {
        "rate_limits": {},
        "tokens": totals,
        "cost_tokens_by_model": by_model,
        "token_scope": "local_daily",
        "model": latest_model,
        "detected_model": latest_model,
        "provider": "anthropic",
        "source_id": source_registry.CLAUDE_CODE_SOURCE,
        "latest_epoch": latest_epoch,
        "activity_epoch": latest_epoch,
        "source": latest_source,
        "reset_count": 0,
        "reset_count_source": "unavailable",
        "activity": "thinking" if now - latest_epoch < 20 else "idle",
    }


def read_latest_usage(
    codex_home: Path,
    source_selector: str = source_registry.AUTO_SOURCE,
    claude_home: Path | None = None,
    account_cache: Path | None = None,
    *,
    force_account: bool = False,
) -> dict[str, Any]:
    """Read one selected AI coding tool, or whichever was active most recently."""
    selected = source_registry.normalize_source_id(source_selector)
    claude_home = claude_home or Path.home() / ".claude"
    if selected == source_registry.CODEX_SOURCE:
        return _read_codex_usage(
            codex_home, account_cache, force_account=force_account
        )
    if selected == source_registry.CLAUDE_CODE_SOURCE:
        return _read_claude_code_usage(claude_home)
    if selected != source_registry.AUTO_SOURCE:
        raise ValueError(f"Unsupported AI tool source: {source_selector}")

    snapshots: list[dict[str, Any]] = []
    try:
        snapshots.append(
            _read_codex_usage(
                codex_home, account_cache, force_account=force_account
            )
        )
    except (FileNotFoundError, codex_account.CodexAccountError):
        pass
    try:
        snapshots.append(_read_claude_code_usage(claude_home))
    except FileNotFoundError:
        pass
    if not snapshots:
        raise FileNotFoundError(
            "No Codex or Claude Code usage data was found. Run at least one request."
        )
    # In Auto mode, an actionable Codex approval request must not be hidden by
    # a slightly newer Claude log entry. Explicit Claude selection remains
    # isolated because it returns before reaching this aggregation.
    for snapshot in snapshots:
        if (
            snapshot.get("source_id") == source_registry.CODEX_SOURCE
            and snapshot.get("needs_approval")
        ):
            return snapshot
    return max(snapshots, key=lambda item: float(item.get("latest_epoch", 0)))


def usage_fingerprint(
    codex_home: Path, claude_home: Path, source_selector: str
) -> tuple[Any, ...]:
    """Return cheap metadata that changes whenever relevant local logs change."""
    selected = source_registry.normalize_source_id(source_selector)
    paths: list[Path] = []
    if selected in (source_registry.AUTO_SOURCE, source_registry.CODEX_SOURCE):
        paths.extend(_candidate_files(codex_home))
    if selected in (
        source_registry.AUTO_SOURCE, source_registry.CLAUDE_CODE_SOURCE
    ):
        paths.extend(_claude_candidate_files(claude_home))
    metadata: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            metadata.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            continue
    return selected, tuple(metadata)


def _refresh_activity(snapshot: dict[str, Any]) -> None:
    if snapshot.get("needs_approval"):
        snapshot["activity"] = "need approval"
        return
    task_active = snapshot.get("task_active")
    if task_active is not None:
        snapshot["activity"] = "thinking" if task_active else "idle"
        return
    # Modern Codex logs provide explicit task_started/task_complete events.
    # Do not keep THINKING alive merely because a token_count was written
    # recently: the UI state must represent an actually running task.
    if snapshot.get("source_id") == source_registry.CODEX_SOURCE:
        snapshot["activity"] = "idle"
        return
    activity_epoch = float(snapshot.get("activity_epoch") or 0)
    snapshot["activity"] = (
        "thinking" if activity_epoch > 0 and time.time() - activity_epoch < 20
        else "idle"
    )


def _next_sequence() -> int:
    """Return a process-local monotonic uint32 sequence, even within one ms."""
    global _last_sequence
    candidate = int(time.time() * 1000) & 0xFFFFFFFF
    with _sequence_lock:
        clock_moved_back = (
            candidate < _last_sequence
            and _last_sequence - candidate < 0x80000000
        )
        if candidate == _last_sequence or clock_moved_back:
            candidate = (_last_sequence + 1) & 0xFFFFFFFF
        _last_sequence = candidate
        return candidate


def _rate_limit_reached(limits: dict[str, Any]) -> bool:
    if limits.get("rate_limit_reached_type"):
        return True
    for window in (limits.get("primary"), limits.get("secondary")):
        try:
            # The UI displays the remaining quota (100 - used). Exactly 10%
            # remaining is still normal; anything below it enters the alert
            # treatment from the LIMIT REACHED Figma frame.
            if 100.0 - float(
                (window or {}).get("used_percent", 0)
            ) < 10.0:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def _device_quota_windows(
    limits: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Map official window positions to the device's short/weekly slots.

    App Server ``primary`` and ``secondary`` are not aliases for 5H and WEEK.
    In particular, accounts without a short window currently return the
    10080-minute weekly window as primary and null as secondary.
    """
    short_window: dict[str, Any] | None = None
    weekly_window: dict[str, Any] | None = None
    for value in (limits.get("primary"), limits.get("secondary")):
        if not isinstance(value, dict) or not value:
            continue
        minutes = _safe_int(value.get("window_minutes"))
        if minutes >= 10080:
            if weekly_window is None:
                weekly_window = value
        elif short_window is None:
            short_window = value
    return short_window, weekly_window


def selected_quota_signature(snapshot: dict[str, Any]) -> str:
    """Stable signature for the selected bucket only, excluding model buckets."""
    limits = snapshot.get("rate_limits") or {}
    value = {
        "limit_id": limits.get("limit_id") or codex_account.MAIN_LIMIT_ID,
        "primary": limits.get("primary"),
        "secondary": limits.get("secondary"),
        "plan_type": limits.get("plan_type"),
        "rate_limit_reached_type": limits.get("rate_limit_reached_type"),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def build_test_snapshot() -> tuple[dict[str, Any], float]:
    """Return deterministic data; the device advances states on each tap."""
    now = int(time.time())
    snapshot = {
        "rate_limits": {
            "primary": {
                "used_percent": 50,
                "window_minutes": 300,
                "resets_at": now + 82 * 60,
                "tokens_used": 1_120_184,
            },
            "secondary": {
                "used_percent": 0,
                "window_minutes": 10080,
                "resets_at": now + 6 * 86400 + 22 * 3600,
                "tokens_used": 2_111_540_267,
            },
            "plan_type": "prolite",
            "rate_limit_reached_type": None,
        },
        "tokens": {
            "input_tokens": 2_104_431_078,
            "cached_input_tokens": 1_982_744_812,
            "output_tokens": 7_109_189,
            "reasoning_output_tokens": 2_291_000,
            "total_tokens": 2_111_540_267,
        },
        "model": "gpt-5.6-sol",
        "provider": "openai",
        "reset_count": 3,
        "reset_count_source": "test",
        "activity": "idle",
        "token_scope": "local_daily",
        "test_state": TEST_MODE_INITIAL_STATE,
        "source": "CodexMeterSync test mode",
    }
    return snapshot, 1829.34


def build_packet(
    snapshot: dict[str, Any], account_label: str = "", cost_cny: float | None = None
) -> bytes:
    now = int(time.time())
    sequence = _next_sequence()
    limits = snapshot.get("rate_limits") or {}
    short_window, weekly_window = _device_quota_windows(limits)
    totals = snapshot.get("tokens") or {}

    reached = _rate_limit_reached(limits)
    # Keep task lifecycle and quota state as separate signals. A task banner
    # (including NEED APPROVAL) can coexist with a low-quota card.
    status = (
        "need approval" if snapshot.get("needs_approval")
        else snapshot.get("activity", "idle")
    )

    def window(value: dict[str, Any]) -> list[int] | None:
        if not value:
            return None
        return [
            _device_used_percent(value.get("used_percent")),
            _bounded_u16(value.get("window_minutes")),
            _bounded_u32(value.get("resets_at")),
            _bounded_u32(value.get("tokens_used")),
        ]

    message = {
        "v": PROTOCOL_VERSION,
        "n": sequence,
        "ts": now,
        "q": [window(short_window or {}), window(weekly_window or {})],
        "t": [
            _bounded_u64(totals.get("input_tokens")),
            _bounded_u64(totals.get("cached_input_tokens")),
            _bounded_u64(totals.get("output_tokens")),
            _bounded_u64(totals.get("reasoning_output_tokens")),
            _bounded_u64(totals.get("total_tokens")),
        ] if totals else [],
        "p": _bounded_text(limits.get("plan_type"), 15),
        "pr": _bounded_text(snapshot.get("provider"), 11),
        "m": _bounded_text(snapshot.get("model"), 23),
        "a": _bounded_text(account_label, 19),
        "s": _bounded_text(status, 31),
        "ap": bool(snapshot.get("needs_approval")),
        "r": reached,
        "af": not bool(snapshot.get(
            "device_data_stale",
            snapshot.get("account_data_stale", False),
        )),
        "lt": snapshot.get("token_scope") == "account_lifetime",
        "rc": _bounded_u16(snapshot.get("reset_count")),
        "rcv": bool(
            snapshot.get(
                "reset_count_available",
                snapshot.get("reset_count_source") != "unavailable",
            )
        ),
        "c": -1 if cost_cny is None else max(0, round(cost_cny * 100)),
    }
    test_state = max(0, min(255, _safe_int(snapshot.get("test_state"))))
    if test_state:
        message["x"] = test_state
    packet = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(packet) > MAX_JSON_SIZE:
        raise ValueError(f"Encoded JSON is {len(packet)} bytes; maximum is {MAX_JSON_SIZE}")
    return packet


def print_snapshot(
    snapshot: dict[str, Any], packet: bytes, cost_cny: float | None,
    usd_cny: float, fx_source: str, cost_source: str
) -> None:
    limits = snapshot.get("rate_limits") or {}
    totals = snapshot.get("tokens") or {}
    summary = {
        "primary": limits.get("primary"),
        "secondary": limits.get("secondary"),
        "plan": limits.get("plan_type"),
        "provider": snapshot.get("provider"),
        "model": snapshot.get("model"),
        "total_tokens": totals.get("total_tokens"),
        "reset_count": snapshot.get("reset_count"),
        "reset_count_available": snapshot.get("reset_count_available"),
        "reset_count_source": snapshot.get("reset_count_source"),
        "activity": snapshot.get("activity"),
        "estimated_cost_cny": None if cost_cny is None else round(cost_cny, 4),
        "usd_cny": usd_cny,
        "fx_source": fx_source,
        "cost_source": cost_source,
        "wire_format": "compact-json-v1",
        "message_bytes": len(packet),
        "message": json.loads(packet),
        "source": snapshot.get("source"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


async def _scan_meter_devices(timeout: float):
    from bleak import BleakScanner

    found: dict[str, tuple[Any, str]] = {}

    def detection_callback(device, advertisement_data) -> None:
        local_name = advertisement_data.local_name or device.name or ""
        if not is_meter_advertisement(
                local_name, advertisement_data.service_uuids):
            return
        address = str(device.address)
        found[address] = (device, device_display_name(local_name, address))

    scanner = BleakScanner(detection_callback=detection_callback)
    await scanner.start()
    try:
        await asyncio.sleep(max(0.1, timeout))
    finally:
        await scanner.stop()
    return sorted(found.values(), key=lambda item: (item[1], str(item[0].address)))


async def discover_devices(timeout: float = 5.0) -> list[dict[str, str]]:
    matches = await _scan_meter_devices(timeout)
    return [
        {"address": str(device.address), "name": name}
        for device, name in matches
    ]


async def find_device(
    address: str | None,
    timeout: float,
    discovery_callback: Callable[[list[dict[str, str]]], None] | None = None,
):
    from bleak import BleakScanner

    # A stored or explicitly supplied address is a strict binding. Never fall
    # back to a nearby device if that target is powered off.
    if address:
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    matches = await _scan_meter_devices(timeout)
    devices = [
        {"address": str(device.address), "name": name}
        for device, name in matches
    ]
    if discovery_callback:
        try:
            discovery_callback(devices)
        except Exception:
            pass
    if len(matches) > 1:
        raise MultipleDevicesFoundError(devices)
    return matches[0][0] if matches else None


def packet_signature(packet: bytes) -> str:
    message = json.loads(packet)
    message.pop("n", None)
    message.pop("ts", None)
    return json.dumps(message, sort_keys=True, separators=(",", ":"))


def should_transmit_packet(
    signature: str,
    last_signature: str | None,
    *,
    force_send: bool = False,
    heartbeat_due: bool = False,
) -> bool:
    """Gate display-data writes while retaining an explicit link keepalive."""
    return (
        force_send
        or heartbeat_due
        or last_signature is None
        or signature != last_signature
    )


def _ack_handler(queue: asyncio.Queue[int]):
    loop = asyncio.get_running_loop()

    def handle(_, data: bytearray) -> None:
        if len(data) != ACK_SIZE or data[0] != PROTOCOL_VERSION:
            return
        sequence = int.from_bytes(data[1:5], "little")
        loop.call_soon_threadsafe(queue.put_nowait, sequence)

    return handle


async def write_packet_with_ack(client, packet: bytes,
                                ack_queue: asyncio.Queue[int],
                                timeout: float = 5.0) -> None:
    while not ack_queue.empty():
        with suppress(asyncio.QueueEmpty):
            ack_queue.get_nowait()
    sequence = _safe_int(json.loads(packet).get("n")) & 0xFFFFFFFF
    await client.write_gatt_char(DATA_CHARACTERISTIC_UUID, packet, response=True)
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(f"Device ACK timeout for sequence {sequence}")
        try:
            ack_sequence = await asyncio.wait_for(ack_queue.get(), remaining)
        except TimeoutError as exc:
            # asyncio.TimeoutError has an empty message; keep the failed
            # protocol stage visible rather than logging a blank reconnect.
            raise TimeoutError(f"Device ACK timeout for sequence {sequence}") from exc
        if ack_sequence == sequence:
            return


def connection_error_detail(stage: str, error: Exception) -> str:
    detail = str(error).strip()
    return f"{stage}: {type(error).__name__}" + (f": {detail}" if detail else "")


def _client_options() -> dict[str, Any]:
    return (
        {"winrt": {"use_cached_services": False}}
        if sys.platform == "win32" else {}
    )


async def send_once(packet: bytes, address: str | None, timeout: float) -> str:
    from bleak import BleakClient

    device = await find_device(address, timeout)
    if device is None:
        raise TimeoutError(f"Could not find BLE device '{DEVICE_NAME}'")

    async with BleakClient(device, timeout=timeout, **_client_options()) as client:
        ack_queue: asyncio.Queue[int] = asyncio.Queue()
        await client.start_notify(ACK_CHARACTERISTIC_UUID, _ack_handler(ack_queue))
        await write_packet_with_ack(client, packet, ack_queue)
        return str(device.address)


async def watch_usage_files(codex_home: Path, claude_home: Path,
                            changed: asyncio.Event,
                            stop_event: threading.Event,
                            debounce_seconds: float,
                            status_callback: Callable[[str], None] | None) -> None:
    try:
        from watchfiles import awatch
    except ImportError:
        _emit("watchfiles is unavailable; using the 5-second reconciliation fallback",
              status_callback, error=True)
        return

    codex_sessions = codex_home / "sessions"
    claude_projects = claude_home / "projects"
    folders = [
        folder for folder in (codex_sessions, claude_projects)
        if folder.exists()
    ]
    if not folders:
        return
    async for changes in awatch(
        *folders,
        debounce=max(500, round(debounce_seconds * 1000)),
        step=100,
    ):
        # Do not pass the service-wide stop event into watchfiles. When an
        # awatch task is cancelled (for example, after a BLE disconnect),
        # watchfiles sets the supplied event to wake its worker thread. Using
        # the shared event here therefore stopped the entire sync service and
        # prevented every subsequent BLE reconnect.
        if stop_event.is_set():
            return
        jsonl_paths = [
            Path(path) for _, path in changes
            if str(path).lower().endswith(".jsonl")
        ]
        if jsonl_paths:
            for path in jsonl_paths:
                _mark_codex_path_dirty(codex_home, path)
                try:
                    path.resolve().relative_to(claude_projects.resolve())
                except (OSError, ValueError):
                    continue
                _mark_claude_candidates_dirty(claude_home)
            changed.set()


def _sync_summary(snapshot: dict[str, Any], packet: bytes, address: str,
                  cost_cny: float | None, cost_source: str,
                  usd_cny: float, fx_source: str) -> str:
    short_window, weekly_window = _device_quota_windows(
        snapshot.get("rate_limits") or {}
    )

    def percentage(window: dict[str, Any] | None) -> str:
        if not window:
            return "--"
        return f"{window.get('used_percent', '--')}%"

    status = (
        "need approval" if snapshot.get("needs_approval")
        else "limited" if _rate_limit_reached(snapshot["rate_limits"])
        else snapshot.get("activity", "idle")
    )
    reset_available = bool(
        snapshot.get(
            "reset_count_available",
            snapshot.get("reset_count_source") != "unavailable",
        )
    )
    reset_value = (
        str(snapshot.get("reset_count", 0)) if reset_available else "--"
    )
    account_detail = ""
    if snapshot.get("source_id") == source_registry.CODEX_SOURCE:
        rate_fetched_at = (
            _valid_positive_number(snapshot.get("rate_limits_fetched_at"))
            or 0
        )
        quota_age = (
            "unknown"
            if rate_fetched_at <= 0
            else f"{max(0, round(time.time() - rate_fetched_at))}s"
        )
        device_stale = bool(snapshot.get(
            "device_data_stale",
            snapshot.get("account_data_stale", False),
        ))
        warning = str(snapshot.get("account_error") or "").replace(
            "\n", " "
        ).strip()
        warning_detail = (
            f", refresh warning: {warning[:160]}" if warning else ""
        )
        account_detail = (
            f" | account {'stale' if device_stale else 'fresh'} "
            f"(quota age {quota_age}{warning_detail})"
        )
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"[{stamp}] synced+ACK {len(packet)} JSON bytes to {address}: "
        f"5H {percentage(short_window)} / "
        f"WEEK {percentage(weekly_window)} | status {status} | "
        f"tool {source_registry.selector_label(snapshot.get('source_id'))} | "
        f"resets {reset_value} "
        f"({snapshot.get('reset_count_source', 'unavailable')}) | "
        f"cost {'--' if cost_cny is None else f'RMB {cost_cny:.2f}'} "
        f"({cost_source}, USD/CNY {usd_cny:g} {fx_source})"
        f"{account_detail}"
    )


async def monitor_connection(
    client, address: str, args: argparse.Namespace, codex_home: Path,
    claude_home: Path,
    account_cache: Path, usd_cny: float, fx_source: str,
    stop_event: threading.Event,
    sync_now_event: threading.Event, test_mode_event: threading.Event,
    test_restore_event: threading.Event,
    status_callback: Callable[[str], None] | None,
    reconnect_event: threading.Event | None = None,
    firmware_update_queue: queue.Queue[
        update_manager.FirmwareUpdateRequest
    ] | None = None,
    update_display_queue: queue.Queue[
        update_manager.DeviceUpdateDisplayRequest
    ] | None = None,
    skin_queue: queue.Queue[skin_manager.SkinRequest] | None = None,
) -> None:
    reconnect_event = reconnect_event or threading.Event()
    changed = asyncio.Event()
    changed.set()
    ack_queue: asyncio.Queue[int] = asyncio.Queue()
    await client.start_notify(ACK_CHARACTERISTIC_UUID, _ack_handler(ack_queue))
    watcher = asyncio.create_task(
        watch_usage_files(
            codex_home, claude_home, changed, stop_event, args.debounce,
            status_callback,
        )
    )
    last_signature: str | None = None
    loop = asyncio.get_running_loop()
    next_reconcile = 0.0
    next_heartbeat = 0.0
    was_test_mode = False
    cached_snapshot: dict[str, Any] | None = None
    cached_fingerprint: tuple[Any, ...] | None = None
    cached_source = ""
    last_source_error = ""
    refresh_lock = asyncio.Lock()
    transfer_lock = asyncio.Lock()

    async def skin_worker():
        while not stop_event.is_set() and not reconnect_event.is_set():
            request = None
            if skin_queue is not None:
                with suppress(queue.Empty):
                    request = skin_queue.get_nowait()
            if request is None:
                await asyncio.sleep(0.1)
                continue
            result, error = None, None
            try:
                if request.address.casefold() != address.casefold():
                    raise skin_manager.SkinError("设备已改变，请重新选择皮肤")
                async with transfer_lock:
                    if request.read_only:
                        result = await skin_manager.read_status(client)
                    else:
                        result = await skin_manager.transfer(client, request, status_callback,
                            lambda: stop_event.is_set() or reconnect_event.is_set())
            except asyncio.CancelledError:
                error = "连接已中断；请重新连接后确认设备当前皮肤"
                raise
            except Exception as exc:
                error = str(exc)
            finally:
                skin_queue.task_done()
                if callable(request.complete):
                    request.complete(result, error)

    skin_task = asyncio.create_task(skin_worker())

    async def transmit(snapshot: dict[str, Any], *, force_send: bool = False) -> bool:
        nonlocal last_signature, next_heartbeat
        _refresh_activity(snapshot)
        cost_cny, cost_source = cost_cny_for_snapshot(
            snapshot, args.cost_cny, usd_cny
        )
        packet = build_packet(snapshot, args.account_label, cost_cny)
        signature = packet_signature(packet)
        heartbeat_due = loop.time() >= next_heartbeat
        if not should_transmit_packet(
            signature,
            last_signature,
            force_send=force_send,
            heartbeat_due=heartbeat_due,
        ):
            return False
        await write_packet_with_ack(
            client, packet, ack_queue, timeout=args.ack_timeout
        )
        last_signature = signature
        next_heartbeat = loop.time() + max(5.0, args.heartbeat)
        _emit(
            _sync_summary(
                snapshot, packet, address, cost_cny, cost_source,
                usd_cny, fx_source,
            ),
            status_callback,
        )
        return True

    try:
        while (
            client.is_connected
            and not stop_event.is_set()
            and not reconnect_event.is_set()
        ):
            display_request = None
            if update_display_queue is not None:
                with suppress(queue.Empty):
                    display_request = update_display_queue.get_nowait()
            if display_request is not None:
                try:
                    await update_manager.send_device_update_state(
                        client, display_request
                    )
                except Exception as exc:
                    _emit(
                        f"Could not update device display state: {exc}",
                        status_callback,
                        error=True,
                    )
                finally:
                    update_display_queue.task_done()

            firmware_request = None
            if firmware_update_queue is not None and not transfer_lock.locked():
                with suppress(queue.Empty):
                    firmware_request = firmware_update_queue.get_nowait()
            if firmware_request is not None:
                try:
                    async with transfer_lock:
                        await update_manager.perform_ble_firmware_update(
                            client, firmware_request, status_callback
                        )
                except Exception as exc:
                    _emit(
                        f"Firmware update failed: {exc}",
                        status_callback,
                        error=True,
                    )
                else:
                    raise ConnectionError(
                        "Device is restarting after firmware update"
                    )
                finally:
                    firmware_update_queue.task_done()

            now = loop.time()
            forced = sync_now_event.is_set()
            if forced:
                sync_now_event.clear()

            if test_mode_event.is_set():
                if not was_test_mode or forced:
                    snapshot, cost_cny = build_test_snapshot()
                    packet = build_packet(snapshot, args.account_label, cost_cny)
                    await write_packet_with_ack(
                        client, packet, ack_queue, timeout=args.ack_timeout
                    )
                    last_signature = packet_signature(packet)
                    test_restore_event.clear()
                    _emit(
                        f"Test mode active ({len(TEST_STATE_NAMES)} states): "
                        "tap anywhere on the device to advance",
                        status_callback,
                    )
                was_test_mode = True
                await asyncio.sleep(0.2)
                continue

            restoring_live_data = was_test_mode
            if restoring_live_data:
                forced = True
                changed.set()
                last_signature = None
            should_check = (
                forced or changed.is_set() or now >= next_reconcile
                or now >= next_heartbeat
            )
            if should_check:
                async with refresh_lock:
                    file_event = changed.is_set()
                    changed.clear()
                    next_reconcile = now + max(1.0, args.interval)
                    selected_source = source_registry.normalize_source_id(args.source)

                    # Status fast path consumes only newly appended bytes and
                    # sends cached counters before the full snapshot update.
                    if (
                        file_event and cached_snapshot is not None
                        and cached_snapshot.get("source_id") == source_registry.CODEX_SOURCE
                        and selected_source in (
                            source_registry.AUTO_SOURCE, source_registry.CODEX_SOURCE
                        )
                    ):
                        (
                            task_active,
                            needs_approval,
                            task_epoch,
                        ) = await asyncio.to_thread(
                            read_codex_runtime_state, codex_home
                        )
                        previous_epoch = float(
                            cached_snapshot.get("task_state_epoch") or float("-inf")
                        )
                        runtime_changed = (
                            task_active != cached_snapshot.get("task_active")
                            or needs_approval != bool(
                                cached_snapshot.get("needs_approval")
                            )
                        )
                        if (
                            task_active is not None
                            and (task_epoch > previous_epoch or runtime_changed)
                        ):
                            fast_snapshot = copy.deepcopy(cached_snapshot)
                            fast_snapshot["task_active"] = task_active
                            fast_snapshot["needs_approval"] = needs_approval
                            fast_snapshot["task_state_epoch"] = task_epoch
                            await transmit(fast_snapshot)
                            cached_snapshot = copy.deepcopy(fast_snapshot)

                    fingerprint = await asyncio.to_thread(
                        usage_fingerprint, codex_home, claude_home, selected_source
                    )
                    local_snapshot_changed = (
                        forced or file_event or selected_source != cached_source
                        or fingerprint != cached_fingerprint
                    )
                    codex_account_poll = (
                        selected_source == source_registry.CODEX_SOURCE
                        or (
                            selected_source == source_registry.AUTO_SOURCE
                            and cached_snapshot is not None
                            and cached_snapshot.get("source_id")
                            == source_registry.CODEX_SOURCE
                        )
                    )
                    needs_read = local_snapshot_changed or codex_account_poll
                    snapshot: dict[str, Any] | None = None
                    if needs_read:
                        try:
                            if (
                                selected_source == source_registry.AUTO_SOURCE
                                and codex_account_poll
                                and not local_snapshot_changed
                            ):
                                # Poll the selected Codex account without
                                # re-reading every unchanged Claude log.
                                snapshot = await asyncio.to_thread(
                                    _read_codex_usage,
                                    codex_home,
                                    account_cache,
                                    force_account=forced,
                                )
                            else:
                                snapshot = await asyncio.to_thread(
                                    read_latest_usage,
                                    codex_home,
                                    selected_source,
                                    claude_home,
                                    account_cache,
                                    force_account=forced,
                                )
                        except (FileNotFoundError, OSError, ValueError) as exc:
                            cached_snapshot = None
                            cached_fingerprint = fingerprint
                            cached_source = selected_source
                            message = str(exc)
                            if message != last_source_error:
                                _emit(
                                    f"Waiting for {source_registry.selector_label(selected_source)} "
                                    f"data: {message}",
                                    status_callback, error=True,
                                )
                                last_source_error = message
                        else:
                            cached_fingerprint = fingerprint
                            cached_source = selected_source
                            last_source_error = ""
                    elif cached_snapshot is not None:
                        snapshot = copy.deepcopy(cached_snapshot)

                    if snapshot is None:
                        next_heartbeat = now + max(5.0, args.heartbeat)
                    else:
                        cached_snapshot = copy.deepcopy(snapshot)
                        sent = await transmit(snapshot, force_send=forced)
                        if sent and restoring_live_data:
                            test_restore_event.set()
                            was_test_mode = False

            now = loop.time()
            deadline = min(next_reconcile, next_heartbeat)
            wait_seconds = max(0.1, min(1.0, deadline - now))
            try:
                await asyncio.wait_for(changed.wait(), wait_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        skin_task.cancel()
        with suppress(asyncio.CancelledError):
            await skin_task
        watcher.cancel()
        with suppress(asyncio.CancelledError):
            await watcher


async def _sleep_until_retry(seconds: float,
                             stop_event: threading.Event,
                             wake_event: threading.Event | None = None) -> None:
    deadline = asyncio.get_running_loop().time() + seconds
    while (
        not stop_event.is_set()
        and not (wake_event and wake_event.is_set())
        and asyncio.get_running_loop().time() < deadline
    ):
        await asyncio.sleep(min(0.25, max(0.0, deadline - asyncio.get_running_loop().time())))


async def run(
    args: argparse.Namespace,
    *,
    status_callback: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    sync_now_event: threading.Event | None = None,
    test_mode_event: threading.Event | None = None,
    test_restore_event: threading.Event | None = None,
    device_change_event: threading.Event | None = None,
    device_list_callback: Callable[[list[dict[str, str]]], None] | None = None,
    binding_callback: Callable[[str, str], None] | None = None,
    device_info_callback: Callable[[dict[str, Any]], None] | None = None,
    firmware_update_queue: queue.Queue[
        update_manager.FirmwareUpdateRequest
    ] | None = None,
    update_display_queue: queue.Queue[
        update_manager.DeviceUpdateDisplayRequest
    ] | None = None,
    skin_queue: queue.Queue[skin_manager.SkinRequest] | None = None,
) -> int:
    codex_home = Path(args.codex_home).expanduser().resolve()
    claude_home = Path(args.claude_home).expanduser().resolve()
    data_dir = state_directory()
    address_cache = data_dir / ".device-address"
    device_name_cache = data_dir / ".device-name"
    fx_cache = data_dir / ".exchange-rates.json"
    account_cache = data_dir / ".codex-account.json"
    stop_event = stop_event or threading.Event()
    sync_now_event = sync_now_event or threading.Event()
    test_mode_event = test_mode_event or threading.Event()
    test_restore_event = test_restore_event or threading.Event()
    device_change_event = device_change_event or threading.Event()
    usd_cny, fx_source = resolve_usd_cny(
        fx_cache, args.usd_cny, allow_live=not args.no_live_fx
    )
    preferred_address = args.address
    if not preferred_address and address_cache.exists():
        preferred_address = address_cache.read_text(encoding="utf-8").strip() or None
    args.address = preferred_address

    if args.dry_run:
        snapshot = read_latest_usage(
            codex_home,
            args.source,
            claude_home,
            account_cache,
            force_account=True,
        )
        cost_cny, cost_source = cost_cny_for_snapshot(
            snapshot, args.cost_cny, usd_cny
        )
        packet = build_packet(snapshot, args.account_label, cost_cny)
        print_snapshot(
            snapshot, packet, cost_cny, usd_cny, fx_source, cost_source
        )
        return 0

    if args.once:
        try:
            snapshot = read_latest_usage(
                codex_home,
                args.source,
                claude_home,
                account_cache,
                force_account=True,
            )
            cost_cny, cost_source = cost_cny_for_snapshot(
                snapshot, args.cost_cny, usd_cny
            )
            packet = build_packet(snapshot, args.account_label, cost_cny)
            address = await send_once(packet, preferred_address, args.scan_timeout)
            _write_text_best_effort(address_cache, address)
            _emit(
                _sync_summary(
                    snapshot, packet, address, cost_cny, cost_source,
                    usd_cny, fx_source,
                ),
                status_callback,
            )
            return 0
        except Exception as exc:
            _emit(f"sync failed: {exc}", status_callback, error=True)
            return 1

    from bleak import BleakClient

    backoff = 1.0
    while not stop_event.is_set():
        if device_change_event.is_set():
            device_change_event.clear()
            preferred_address = args.address
            backoff = 1.0
        stage = "device discovery"
        try:
            if preferred_address:
                _emit(
                    f"Searching for bound device "
                    f"{device_display_name(None, preferred_address)}...",
                    status_callback,
                )
            else:
                _emit("Searching for Codex Meter...", status_callback)
            device = await find_device(
                preferred_address,
                args.scan_timeout if preferred_address else min(5.0, args.scan_timeout),
                device_list_callback,
            )
            if device is None:
                if preferred_address:
                    raise TimeoutError(
                        f"Bound device {device_display_name(None, preferred_address)} "
                        "is unavailable"
                    )
                raise TimeoutError(f"Could not find BLE device '{DEVICE_NAME}'")
            stage = "BLE connect/service discovery"
            async with BleakClient(
                device, timeout=args.scan_timeout, **_client_options()
            ) as client:
                preferred_address = str(device.address)
                device_name = device_display_name(
                    getattr(device, "name", None), preferred_address
                )
                args.address = preferred_address
                _write_text_best_effort(address_cache, preferred_address)
                _write_text_best_effort(device_name_cache, device_name)
                if binding_callback:
                    try:
                        binding_callback(preferred_address, device_name)
                    except Exception:
                        pass
                _emit(
                    f"Connected to {device_name} ({preferred_address})",
                    status_callback,
                )
                stage = "device info read"
                device_info = await update_manager.read_device_info(client)
                if device_info.get("skin") == 1:
                    try:
                        device_info["skin_status"] = await skin_manager.read_status(client)
                    except Exception as exc:
                        device_info["skin_error"] = str(exc)
                if device_info_callback:
                    try:
                        device_info_callback(device_info)
                    except Exception:
                        pass
                backoff = 1.0
                stage = "data sync/ACK"
                await monitor_connection(
                    client, preferred_address, args, codex_home, claude_home,
                    account_cache, usd_cny, fx_source, stop_event,
                    sync_now_event,
                    test_mode_event,
                    test_restore_event, status_callback, device_change_event,
                    firmware_update_queue,
                    update_display_queue,
                    skin_queue,
                )
                if device_change_event.is_set():
                    preferred_address = args.address
                    device_change_event.clear()
                    backoff = 1.0
                    continue
                if not stop_event.is_set():
                    raise ConnectionError("BLE connection closed")
        except Exception as exc:
            if stop_event.is_set():
                break
            if device_change_event.is_set():
                preferred_address = args.address
                device_change_event.clear()
                backoff = 1.0
                continue
            _emit(f"sync reconnecting: {connection_error_detail(stage, exc)}",
                  status_callback, error=True)
            await _sleep_until_retry(backoff, stop_event, device_change_event)
            backoff = min(30.0, backoff * 2.0)
    _emit("Sync stopped", status_callback)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_home = os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))
    default_claude_home = os.environ.get(
        "CLAUDE_CONFIG_DIR", str(Path.home() / ".claude")
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-home", default=default_home)
    parser.add_argument("--claude-home", default=default_claude_home)
    parser.add_argument("--account-label", default="", help="Optional label shown on-device")
    parser.add_argument(
        "--source",
        default=os.environ.get("CODEX_METER_SOURCE", source_registry.AUTO_SOURCE),
        help="AI coding tool: auto, codex, or claude-code",
    )
    parser.add_argument("--model", dest="legacy_model", help=argparse.SUPPRESS)
    parser.add_argument(
        "--cost-cny", type=float,
        help="Override the automatically estimated daily CNY cost",
    )
    parser.add_argument(
        "--usd-cny", type=float,
        help="Manual USD/CNY rate; otherwise use live/cache/fallback precedence",
    )
    parser.add_argument(
        "--no-live-fx", action="store_true",
        help="Do not fetch an exchange rate; use cache or the 6.8 fallback",
    )
    parser.add_argument("--address", help="Known BLE address; skips name-based selection")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--heartbeat", type=float, default=30.0)
    parser.add_argument("--debounce", type=float, default=0.75)
    parser.add_argument("--ack-timeout", type=float, default=5.0)
    parser.add_argument("--scan-timeout", type=float, default=12.0)
    parser.add_argument("--once", action="store_true", help="Exit after one sync attempt")
    parser.add_argument("--dry-run", action="store_true", help="Read and encode data without BLE")
    args = parser.parse_args(argv)
    legacy_model = args.legacy_model or os.environ.get("CODEX_METER_MODEL")
    if legacy_model and source_registry.normalize_source_id(args.source) == \
            source_registry.AUTO_SOURCE:
        args.source = source_registry.normalize_source_id(legacy_model)
    else:
        args.source = source_registry.normalize_source_id(args.source)
    return args


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(run(parse_args())))
    except KeyboardInterrupt:
        print("stopped")
