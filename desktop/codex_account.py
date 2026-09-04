"""Read account quota and token usage through the official Codex App Server."""

from __future__ import annotations

import atexit
import copy
from contextlib import suppress
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


MAIN_LIMIT_ID = "codex"
RATE_LIMIT_REFRESH_SECONDS = 5.0
USAGE_REFRESH_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 5.0
ACCOUNT_STALE_AFTER_SECONDS = 120.0
FAILURE_BACKOFF_MIN_SECONDS = 5.0
FAILURE_BACKOFF_MAX_SECONDS = 60.0
IDENTITY_RECHECK_DURING_BACKOFF_SECONDS = 15.0
CLOCK_SKEW_TOLERANCE_SECONDS = 5.0
CACHE_SCHEMA = 2


class CodexAccountError(OSError):
    """Raised when official account data cannot be read or recovered."""


class CodexTransportError(CodexAccountError):
    """Raised when the local App Server process or JSONL transport fails."""


class CodexRpcError(CodexAccountError):
    """Raised for a stable error response from an App Server method."""


def _field(value: dict[str, Any], camel: str, snake: str) -> Any:
    if camel in value:
        return value[camel]
    return value.get(snake)


def _nonnegative_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return default


def _percentage(value: Any) -> float:
    try:
        return max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _timestamp(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if result >= 0 else 0.0


def _data_age(now: float, value: Any) -> float:
    """Return a safe wall-clock age; future cache stamps must be refreshed."""
    stamp = _timestamp(value)
    if stamp <= 0 or stamp > now + CLOCK_SKEW_TOLERANCE_SECONDS:
        return float("inf")
    return max(0.0, now - stamp)


def normalize_window(value: Any) -> dict[str, Any] | None:
    """Normalize one official quota window without changing its duration."""
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    return {
        "used_percent": _percentage(
            _field(value, "usedPercent", "used_percent")
        ),
        "window_minutes": _nonnegative_int(
            _field(value, "windowDurationMins", "window_minutes")
        ),
        "resets_at": _nonnegative_int(
            _field(value, "resetsAt", "resets_at")
        ),
        # The official account endpoint does not expose this value. Keep the
        # protocol field deterministic instead of deriving it from sessions.
        "tokens_used": 0,
    }


def normalize_bucket(
    value: dict[str, Any], *, default_limit_id: str = "",
    default_plan_type: Any = None,
) -> dict[str, Any]:
    limit_id = _field(value, "limitId", "limit_id")
    limit_name = _field(value, "limitName", "limit_name")
    plan_type = _field(value, "planType", "plan_type")
    if plan_type is None:
        plan_type = default_plan_type
    return {
        "limit_id": str(limit_id or default_limit_id),
        "limit_name": None if limit_name is None else str(limit_name),
        "primary": normalize_window(value.get("primary")),
        "secondary": normalize_window(value.get("secondary")),
        "plan_type": str(plan_type or ""),
        "rate_limit_reached_type": _field(
            value, "rateLimitReachedType", "rate_limit_reached_type"
        ),
    }


def normalize_rate_limits_result(result: Any) -> dict[str, Any]:
    """Select only the exact ``codex`` bucket and retain all buckets by id."""
    if not isinstance(result, dict):
        raise CodexAccountError(
            "account/rateLimits/read returned an invalid result"
        )

    root_plan_type = _field(result, "planType", "plan_type")
    raw_buckets = _field(
        result, "rateLimitsByLimitId", "rate_limits_by_limit_id"
    )
    normalized_buckets: dict[str, dict[str, Any]] = {}
    if isinstance(raw_buckets, dict):
        for key, raw_bucket in raw_buckets.items():
            if not isinstance(raw_bucket, dict):
                continue
            bucket_key = str(key)
            normalized_bucket = normalize_bucket(
                raw_bucket,
                default_limit_id=bucket_key,
                default_plan_type=root_plan_type,
            )
            # The map key is the authoritative metered identifier.
            normalized_bucket["limit_id"] = bucket_key
            normalized_buckets[bucket_key] = normalized_bucket

    selected = normalized_buckets.get(MAIN_LIMIT_ID)
    if selected is None:
        raise CodexAccountError(
            'account/rateLimits/read rateLimitsByLimitId did not return the '
            'required exact "codex" quota bucket'
        )

    reset_credits = _field(
        result, "rateLimitResetCredits", "rate_limit_reset_credits"
    )
    reset_metadata: dict[str, Any] = {
        "reset_count": 0,
        "reset_count_source": "unavailable",
        "reset_count_available": False,
    }
    if isinstance(reset_credits, dict):
        raw_available_count = _field(
            reset_credits, "availableCount", "available_count"
        )
        if raw_available_count is not None:
            reset_metadata = {
                "reset_count": min(
                    0xFFFF, _nonnegative_int(raw_available_count)
                ),
                "reset_count_source": "account/rateLimits/read",
                "reset_count_available": True,
            }

    normalized = {
        "selected_limit_id": MAIN_LIMIT_ID,
        "rate_limits": copy.deepcopy(selected),
        "rate_limits_by_limit_id": normalized_buckets,
    }
    normalized.update(reset_metadata)
    return normalized


def normalize_usage_result(result: Any) -> dict[str, Any]:
    """Normalize account/usage/read; lifetimeTokens is the display total."""
    if not isinstance(result, dict):
        raise CodexAccountError(
            "account/usage/read returned an invalid result"
        )
    raw_summary = result.get("summary")
    summary = raw_summary if isinstance(raw_summary, dict) else {}
    lifetime_value = _field(summary, "lifetimeTokens", "lifetime_tokens")
    tokens: dict[str, int] = {}
    if lifetime_value is not None:
        tokens = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "total_tokens": _nonnegative_int(lifetime_value),
        }

    normalized_summary = {
        "lifetime_tokens": (
            None if lifetime_value is None
            else _nonnegative_int(lifetime_value)
        ),
        "peak_daily_tokens": _field(
            summary, "peakDailyTokens", "peak_daily_tokens"
        ),
        "longest_running_turn_sec": _field(
            summary, "longestRunningTurnSec", "longest_running_turn_sec"
        ),
        "current_streak_days": _field(
            summary, "currentStreakDays", "current_streak_days"
        ),
        "longest_streak_days": _field(
            summary, "longestStreakDays", "longest_streak_days"
        ),
    }

    raw_buckets = _field(
        result, "dailyUsageBuckets", "daily_usage_buckets"
    )
    daily_buckets: list[dict[str, Any]] = []
    if isinstance(raw_buckets, list):
        for raw_bucket in raw_buckets:
            if not isinstance(raw_bucket, dict):
                continue
            start_date = _field(raw_bucket, "startDate", "start_date")
            daily_buckets.append({
                "start_date": str(start_date or ""),
                "tokens": _nonnegative_int(raw_bucket.get("tokens")),
            })

    return {
        "tokens": tokens,
        "token_scope": "account_lifetime",
        "usage_summary": normalized_summary,
        "daily_usage_buckets": daily_buckets,
    }


def account_identity_fingerprint(result: Any) -> str | None:
    """Return an opaque comparison key without persisting the email plaintext."""
    if not isinstance(result, dict):
        raise CodexAccountError("account/read returned an invalid result")
    account = result.get("account")
    if account is None:
        identity: dict[str, Any] = {"type": "signed-out"}
    elif isinstance(account, dict):
        email = str(account.get("email") or "").strip().lower()
        if account.get("type") in ("chatgpt", "chatgptAuthTokens") and not email:
            # The official account shape has no stable account id. Without an
            # email, do not permit a persisted fallback to cross a login
            # change; callers still use one atomic rate+usage read cycle.
            return None
        identity = {
            "type": account.get("type"),
            "email": email,
            "credential_source": _field(
                account, "credentialSource", "credential_source"
            ),
        }
    else:
        raise CodexAccountError("account/read returned an invalid account")
    encoded = json.dumps(
        identity, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CodexAppServerClient:
    """Small persistent JSONL client for the official local App Server."""

    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = (
            codex_home.expanduser().resolve() if codex_home else None
        )
        self._process: subprocess.Popen[str] | None = None
        self._messages: queue.Queue[Any] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._request_id = 0
        self._lock = threading.RLock()

    def _executable(self) -> str:
        override = os.environ.get("CODEX_CLI_PATH", "").strip()
        if override:
            path = Path(override).expanduser()
            if path.is_file():
                return str(path)
            raise CodexAccountError(
                f"CODEX_CLI_PATH does not point to a file: {path}"
            )
        homes = [self.codex_home, Path.home() / ".codex"]
        for home in homes:
            if home is None:
                continue
            bundled = home / ".sandbox-bin" / "codex.exe"
            if bundled.is_file():
                return str(bundled)
        executable = shutil.which("codex") or shutil.which("codex.exe")
        if executable:
            return executable
        raise CodexAccountError(
            "Could not find the Codex executable. Install/open Codex or set "
            "CODEX_CLI_PATH to codex.exe."
        )

    def _reader_main(
        self, process: subprocess.Popen[str], messages: queue.Queue[Any]
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(message, dict):
                    messages.put(message)
        except (OSError, ValueError) as exc:
            messages.put(
                CodexTransportError(f"App Server read failed: {exc}")
            )
        finally:
            messages.put(CodexTransportError("Codex App Server closed"))

    def _send_unlocked(self, message: dict[str, Any]) -> None:
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            raise CodexTransportError("Codex App Server is not running")
        try:
            process.stdin.write(
                json.dumps(message, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise CodexTransportError(
                f"Could not write to Codex App Server: {exc}"
            ) from exc

    def _exchange_unlocked(
        self, method: str, params: dict[str, Any] | None,
        timeout: float,
    ) -> Any:
        self._request_id += 1
        request_id = self._request_id
        message: dict[str, Any] = {
            "method": method,
            "id": request_id,
        }
        if params is not None:
            message["params"] = params
        self._send_unlocked(message)

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexTransportError(
                    f"Codex App Server timed out during {method}"
                )
            try:
                response = self._messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexTransportError(
                    f"Codex App Server timed out during {method}"
                ) from exc
            if isinstance(response, BaseException):
                raise response
            if response.get("method"):
                # This integration does not opt into server-initiated
                # capabilities. Distinguish requests from responses even if
                # their numeric ids happen to collide, and fail unsupported
                # requests promptly instead of leaving App Server blocked.
                if "id" in response:
                    self._send_unlocked({
                        "id": response["id"],
                        "error": {
                            "code": -32601,
                            "message": "Client method is not supported",
                        },
                    })
                continue
            if response.get("id") != request_id:
                # Account update notifications and unrelated server messages
                # do not alter the explicit read response used by this client.
                continue
            error = response.get("error")
            if error:
                if isinstance(error, dict):
                    detail = error.get("message") or json.dumps(error)
                else:
                    detail = str(error)
                raise CodexRpcError(f"{method} failed: {detail}")
            if "result" not in response:
                raise CodexRpcError(
                    f"{method} returned neither result nor error"
                )
            return response["result"]

    def _start_unlocked(self, timeout: float) -> None:
        self._close_unlocked()
        executable = self._executable()
        environment = os.environ.copy()
        if self.codex_home is not None:
            environment["CODEX_HOME"] = str(self.codex_home)
        creation_flags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if sys.platform == "win32" else 0
        )
        startup_info = None
        if sys.platform == "win32":
            startup_info = subprocess.STARTUPINFO()
            startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup_info.wShowWindow = 0
        try:
            process = subprocess.Popen(
                [executable, "app-server", "--listen", "stdio://"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                env=environment,
                creationflags=creation_flags,
                startupinfo=startup_info,
            )
        except (OSError, ValueError) as exc:
            raise CodexAccountError(
                f"Could not start Codex App Server: {exc}"
            ) from exc
        self._process = process
        self._messages = queue.Queue()
        self._reader = threading.Thread(
            target=self._reader_main,
            args=(process, self._messages),
            name="CodexMeter-AppServer",
            daemon=True,
        )
        self._reader.start()
        self._exchange_unlocked(
            "initialize",
            {
                "clientInfo": {
                    "name": "codex_meter_sync",
                    "title": "Codex Meter Sync",
                    "version": "1.0",
                },
                "capabilities": {
                    "optOutNotificationMethods": [
                        "thread/started",
                        "item/started",
                        "item/completed",
                    ],
                },
            },
            timeout,
        )
        self._send_unlocked({"method": "initialized", "params": {}})

    def request(
        self, method: str, params: dict[str, Any] | None = None,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Any:
        with self._lock:
            last_error: CodexAccountError | None = None
            deadline = time.monotonic() + max(0.1, timeout)
            for attempt in range(2):
                started_now = False
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexTransportError(
                            f"Codex App Server timed out during {method}"
                        )
                    if (
                        self._process is None
                        or self._process.poll() is not None
                    ):
                        started_now = True
                        self._start_unlocked(remaining)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise CodexTransportError(
                            f"Codex App Server timed out during {method}"
                        )
                    return self._exchange_unlocked(
                        method, params, remaining
                    )
                except CodexRpcError:
                    if started_now:
                        self._close_unlocked()
                    raise
                except CodexTransportError as exc:
                    last_error = exc
                    self._close_unlocked()
                    if attempt > 0:
                        break
            assert last_error is not None
            raise last_error

    def _close_unlocked(self) -> None:
        process = self._process
        reader = self._reader
        self._process = None
        self._reader = None
        if process is None:
            return
        with suppress(OSError, ValueError):
            if process.stdin is not None:
                process.stdin.close()
        if process.poll() is None:
            with suppress(OSError, ValueError):
                process.terminate()
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                with suppress(OSError, ValueError):
                    process.kill()
                with suppress(OSError, ValueError, subprocess.TimeoutExpired):
                    process.wait(timeout=0.5)
        if (
            reader is not None
            and reader is not threading.current_thread()
            and reader.is_alive()
        ):
            reader.join(timeout=0.2)

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

_clients_lock = threading.Lock()
_clients: dict[str, CodexAppServerClient] = {}
_snapshot_lock = threading.RLock()
_validated_cache_keys: set[str] = set()
_failure_counts: dict[tuple[str, str], int] = {}
_retry_after: dict[tuple[str, str], float] = {}


def _client_for(codex_home: Path | None) -> CodexAppServerClient:
    resolved = codex_home.expanduser().resolve() if codex_home else None
    key = os.path.normcase(str(resolved)) if resolved else "<default>"
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            client = CodexAppServerClient(resolved)
            _clients[key] = client
        return client


def close_clients() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(close_clients)


def _codex_home_key(codex_home: Path | None) -> str:
    if codex_home is None:
        return "<default>"
    return os.path.normcase(str(codex_home.expanduser().resolve()))


def _read_cache(path: Path, codex_home_key: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != CACHE_SCHEMA:
        return {}
    if value.get("codex_home_key") != codex_home_key:
        return {}
    identity_hash = value.get("account_identity_hash")
    if not isinstance(identity_hash, str) or len(identity_hash) != 64:
        return {}
    if value.get("selected_limit_id") != MAIN_LIMIT_ID:
        return {}
    if not isinstance(value.get("rate_limits"), dict):
        return {}
    if value["rate_limits"].get("limit_id") != MAIN_LIMIT_ID:
        return {}
    if not isinstance(value.get("rate_limits_by_limit_id"), dict):
        return {}
    if MAIN_LIMIT_ID not in value["rate_limits_by_limit_id"]:
        return {}
    for key in (
        "account_identity_checked_at",
        "rate_limits_fetched_at",
        "usage_fetched_at",
    ):
        if key in value and _timestamp(value[key]) == 0:
            return {}
    return value


def _write_cache(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError:
        pass
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _record_failure(cache_key: str, endpoint: str) -> None:
    key = (cache_key, endpoint)
    failures = _failure_counts.get(key, 0) + 1
    _failure_counts[key] = failures
    delay = min(
        FAILURE_BACKOFF_MAX_SECONDS,
        FAILURE_BACKOFF_MIN_SECONDS * (2 ** min(failures - 1, 4)),
    )
    _retry_after[key] = time.monotonic() + delay


def _record_success(cache_key: str, endpoint: str) -> None:
    key = (cache_key, endpoint)
    _failure_counts.pop(key, None)
    _retry_after.pop(key, None)


def _clear_account_scoped_cache(snapshot: dict[str, Any]) -> None:
    for key in (
        "rate_limits",
        "rate_limits_by_limit_id",
        "rate_limits_fetched_at",
        "rate_limits_stale",
        "rate_limit_data_stale",
        "tokens",
        "token_scope",
        "usage_summary",
        "daily_usage_buckets",
        "usage_fetched_at",
        "usage_stale",
        "usage_data_stale",
        "reset_count",
        "reset_count_available",
        "reset_count_source",
        "account_data_stale",
    ):
        snapshot.pop(key, None)


def read_account_snapshot(
    cache_path: Path, codex_home: Path | None = None, *,
    force: bool = False,
) -> dict[str, Any]:
    """Return the exact main Codex bucket and official account usage.

    Auxiliary buckets are preserved in ``rate_limits_by_limit_id`` but are
    never substituted for ``rate_limits``. A safe normalized cache keeps the
    last known values available while the local App Server is temporarily
    unavailable.
    """
    with _snapshot_lock:
        now = time.time()
        home_key = _codex_home_key(codex_home)
        cache_key = (
            os.path.normcase(str(cache_path.expanduser().resolve()))
            + "|"
            + home_key
        )
        first_process_validation = cache_key not in _validated_cache_keys
        _validated_cache_keys.add(cache_key)
        snapshot = _read_cache(cache_path, home_key)
        rate_age = _data_age(
            now, snapshot.get("rate_limits_fetched_at")
        )
        usage_age = _data_age(now, snapshot.get("usage_fetched_at"))
        rate_due_base = force or first_process_validation or not snapshot or (
            rate_age >= RATE_LIMIT_REFRESH_SECONDS
        )
        usage_due_base = (
            force
            or first_process_validation
            or not snapshot.get("tokens")
            or usage_age >= USAGE_REFRESH_SECONDS
        )
        retry_now = time.monotonic()
        rate_due = rate_due_base and (
            force
            or retry_now >= _retry_after.get(
                (cache_key, "rate_limits"), 0
            )
        )
        rate_refresh_blocked = rate_due_base and not rate_due
        usage_due = usage_due_base and (
            force
            or retry_now >= _retry_after.get((cache_key, "usage"), 0)
        )
        errors: list[str] = []
        client: CodexAppServerClient | None = None
        rate_failed = False
        usage_failed = False
        identity_stale = bool(snapshot.get("account_identity_stale", False))

        identity_checked_at = _timestamp(
            snapshot.get("account_identity_checked_at")
        )
        usage_can_run = usage_due and not rate_refresh_blocked
        identity_needed = (
            rate_due
            or usage_can_run
            or (
                (rate_due_base or usage_due_base)
                and now - identity_checked_at
                >= IDENTITY_RECHECK_DURING_BACKOFF_SECONDS
            )
        )
        if identity_needed:
            identity_allowed = (
                force
                or retry_now >= _retry_after.get(
                    (cache_key, "identity"), 0
                )
            )
            if identity_allowed:
                try:
                    client = _client_for(codex_home)
                    identity_result = client.request(
                        "account/read", {"refreshToken": False}
                    )
                    identity_hash = account_identity_fingerprint(
                        identity_result
                    )
                except CodexTransportError as exc:
                    errors.append(str(exc))
                    identity_stale = True
                    rate_failed = True
                    usage_failed = usage_due
                    _record_failure(cache_key, "identity")
                    _record_failure(cache_key, "rate_limits")
                except (CodexAccountError, OSError, ValueError) as exc:
                    errors.append(str(exc))
                    identity_stale = True
                    rate_failed = True
                    usage_failed = usage_due
                    _record_failure(cache_key, "identity")
                else:
                    identity_completed_at = time.time()
                    previous_identity = snapshot.get(
                        "account_identity_hash"
                    )
                    identity_changed = (
                        bool(snapshot)
                        and previous_identity != identity_hash
                    )
                    identity_unverifiable = identity_hash is None
                    if identity_changed or identity_unverifiable:
                        _clear_account_scoped_cache(snapshot)
                        with suppress(OSError):
                            cache_path.unlink()
                        _record_success(cache_key, "rate_limits")
                        _record_success(cache_key, "usage")
                        rate_due = True
                        usage_due = True
                        rate_refresh_blocked = False
                    if identity_hash is None:
                        snapshot.pop("account_identity_hash", None)
                    else:
                        snapshot["account_identity_hash"] = identity_hash
                    snapshot["account_identity_checked_at"] = (
                        identity_completed_at
                    )
                    identity_stale = False
                    _record_success(cache_key, "identity")
            else:
                identity_stale = True
                rate_failed = True
                usage_failed = usage_due
                errors.append(
                    "account identity check is waiting for retry"
                )

        if rate_due and not rate_failed:
            try:
                client = client or _client_for(codex_home)
                result = client.request("account/rateLimits/read")
                normalized = normalize_rate_limits_result(result)
            except (CodexAccountError, OSError, ValueError) as exc:
                errors.append(str(exc))
                rate_failed = True
                _record_failure(cache_key, "rate_limits")
            else:
                completed_at = time.time()
                snapshot.update(normalized)
                snapshot["rate_limits_fetched_at"] = completed_at
                _record_success(cache_key, "rate_limits")

        usage_blocked = (
            usage_due
            and (
                rate_failed
                or rate_refresh_blocked
                or not snapshot.get("rate_limits")
            )
        )
        if usage_blocked:
            usage_failed = True

        if (
            usage_due
            and not rate_failed
            and not rate_refresh_blocked
            and snapshot.get("rate_limits")
        ):
            try:
                client = client or _client_for(codex_home)
                result = client.request("account/usage/read")
                normalized_usage = normalize_usage_result(result)
            except (CodexAccountError, OSError, ValueError) as exc:
                errors.append(str(exc))
                usage_failed = True
                _record_failure(cache_key, "usage")
            else:
                completed_at = time.time()
                tokens = normalized_usage.pop("tokens")
                if tokens:
                    snapshot.update(normalized_usage)
                    snapshot["tokens"] = tokens
                    snapshot["usage_fetched_at"] = completed_at
                    _record_success(cache_key, "usage")
                else:
                    usage_failed = True
                    _record_failure(cache_key, "usage")
                    errors.append(
                        "account/usage/read did not return "
                        "summary.lifetimeTokens"
                    )

        if not isinstance(snapshot.get("rate_limits"), dict):
            detail = "; ".join(errors) or "no cached account data"
            raise CodexAccountError(
                f"Official Codex account data is unavailable: {detail}"
            )

        snapshot["schema"] = CACHE_SCHEMA
        snapshot["codex_home_key"] = home_key
        snapshot["selected_limit_id"] = MAIN_LIMIT_ID
        snapshot.setdefault("tokens", {})
        snapshot.setdefault("reset_count", 0)
        snapshot.setdefault("reset_count_available", False)
        snapshot.setdefault("reset_count_source", "unavailable")
        snapshot["account_source"] = "Codex App Server"
        snapshot["account_error"] = "; ".join(errors)
        snapshot["account_identity_stale"] = identity_stale
        snapshot["rate_limits_stale"] = (
            rate_failed if rate_due
            else bool(snapshot.get("rate_limits_stale", False))
        )
        snapshot["usage_stale"] = (
            usage_failed if usage_due
            else bool(snapshot.get("usage_stale", False))
        )
        rate_last_success = _timestamp(
            snapshot.get("rate_limits_fetched_at")
        )
        usage_last_success = _timestamp(snapshot.get("usage_fetched_at"))
        current_time = time.time()
        # A failed refresh is a warning, not proof that the last successful
        # values are already unusable. Keep serving recent cache entries during
        # transient App Server timeouts and only declare data stale after the
        # explicit age grace period (or when the corresponding data is absent).
        snapshot["rate_limit_data_stale"] = (
            not isinstance(snapshot.get("rate_limits"), dict)
            or _data_age(current_time, rate_last_success)
            > ACCOUNT_STALE_AFTER_SECONDS
        )
        snapshot["usage_data_stale"] = (
            not snapshot.get("tokens")
            or _data_age(current_time, usage_last_success)
            > ACCOUNT_STALE_AFTER_SECONDS
        )
        snapshot["account_data_stale"] = (
            bool(snapshot["rate_limit_data_stale"])
            or bool(snapshot["usage_data_stale"])
        )
        snapshot["fetched_at"] = max(
            rate_last_success,
            usage_last_success,
        )
        _write_cache(cache_path, snapshot)
        return copy.deepcopy(snapshot)
