"""GitHub Releases client updater and BLE firmware OTA transport."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

import release_config


DEVICE_INFO_CHARACTERISTIC_UUID = "7f520004-1b15-4f9f-8bd8-29dbe9e681a1"
OTA_CONTROL_CHARACTERISTIC_UUID = "7f520005-1b15-4f9f-8bd8-29dbe9e681a1"
OTA_DATA_CHARACTERISTIC_UUID = "7f520006-1b15-4f9f-8bd8-29dbe9e681a1"
OTA_STATUS_CHARACTERISTIC_UUID = "7f520007-1b15-4f9f-8bd8-29dbe9e681a1"

USER_AGENT = f"CodexMeterSync/{release_config.APP_VERSION}"
DOWNLOAD_CHUNK_SIZE = 128 * 1024
BLE_FIRMWARE_CHUNK_SIZE = 180
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RELEASE_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")
HTTP_RETRY_ATTEMPTS = 2
MAX_HTTP_ERROR_BODY_BYTES = 512

_api_cooldown_until = 0.0


class UpdateError(RuntimeError):
    pass


class UpdateTransportError(UpdateError):
    pass


class NoPublishedRelease(UpdateError):
    pass


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ComponentRelease:
    version: str
    asset: ReleaseAsset


@dataclass(frozen=True)
class ClientRelease(ComponentRelease):
    pass


@dataclass(frozen=True)
class FirmwareRelease(ComponentRelease):
    board: str
    min_client: str
    min_battery: int


@dataclass(frozen=True)
class UpdateRelease:
    tag: str
    client: ClientRelease
    firmware: FirmwareRelease


@dataclass(frozen=True)
class FirmwareUpdateRequest:
    path: Path
    version: str
    sha256: str
    size: int


@dataclass(frozen=True)
class DeviceUpdateDisplayRequest:
    state: str
    progress: int = 0


DEVICE_UPDATE_STATES = frozenset({
    "IDLE",
    "UPDATE AVAILABLE",
    "DOWNLOADING",
    "INSTALLING",
    "VERIFYING",
    "RESTARTING",
    "UPDATE FAILED",
    "ROLLED BACK",
})


def _version_parts(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lower()
    if text.startswith("v"):
        text = text[1:]
    parts: list[int] = []
    for token in text.split("."):
        match = re.match(r"(\d+)", token)
        parts.append(int(match.group(1)) if match else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def is_newer(candidate: str, current: str) -> bool:
    return _version_parts(candidate) > _version_parts(current)


def _sha256_text(value: Any, label: str) -> str:
    digest = str(value or "").strip().lower()
    if digest.startswith("sha256:"):
        digest = digest.split(":", 1)[1]
    if not HEX_SHA256.fullmatch(digest):
        raise UpdateError(f"{label} has an invalid SHA-256 digest")
    return digest


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(DOWNLOAD_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _is_github_api_url(url: str) -> bool:
    return urlsplit(url).hostname == "api.github.com"


def _header_value(error: HTTPError, name: str) -> str:
    headers = getattr(error, "headers", None)
    if headers is None:
        return ""
    return str(headers.get(name) or "").strip()


def _seconds_header(value: str) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _http_retry_delay(error: HTTPError, attempt: int) -> float | None:
    retry_after = _seconds_header(_header_value(error, "Retry-After"))
    if retry_after is not None:
        return retry_after if retry_after <= 5.0 else None
    if 500 <= error.code <= 599:
        return min(5.0, 0.5 * (2 ** attempt))
    if (
        error.code == 403
        and _header_value(error, "X-RateLimit-Remaining") == "0"
    ):
        reset = _seconds_header(_header_value(error, "X-RateLimit-Reset"))
        if reset is not None:
            remaining = max(0.0, reset - time.time())
            if remaining <= 5.0:
                return remaining
    return None


def _record_api_cooldown(url: str, error: HTTPError) -> None:
    global _api_cooldown_until
    if not _is_github_api_url(url) or error.code not in (403, 429):
        return
    now = time.time()
    retry_after = _seconds_header(_header_value(error, "Retry-After"))
    reset = _seconds_header(_header_value(error, "X-RateLimit-Reset"))
    if retry_after is not None:
        _api_cooldown_until = max(_api_cooldown_until, now + retry_after)
    elif reset is not None:
        _api_cooldown_until = max(_api_cooldown_until, reset)
    elif _header_value(error, "X-RateLimit-Remaining") == "0":
        _api_cooldown_until = max(_api_cooldown_until, now + 60.0)
    else:
        _api_cooldown_until = max(_api_cooldown_until, now + 60.0)


def _http_error_message(error: HTTPError, url: str) -> str:
    parsed = urlsplit(url)
    endpoint = f"{parsed.netloc}{parsed.path}"
    details: list[str] = []
    remaining = _header_value(error, "X-RateLimit-Remaining")
    if remaining:
        details.append(f"rate remaining {remaining}")
    reset = _seconds_header(_header_value(error, "X-RateLimit-Reset"))
    if reset is not None:
        try:
            reset_at = datetime.fromtimestamp(
                reset, timezone.utc
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        except (OSError, OverflowError, ValueError):
            reset_at = str(int(reset))
        details.append(f"rate reset {reset_at}")
    retry_after = _header_value(error, "Retry-After")
    if retry_after:
        details.append(f"retry after {retry_after}s")
    request_id = _header_value(error, "X-GitHub-Request-Id")
    if request_id:
        details.append(f"request {request_id}")
    try:
        body = error.read(MAX_HTTP_ERROR_BODY_BYTES).decode(
            "utf-8", errors="replace"
        )
    except (OSError, ValueError):
        body = ""
    body = " ".join(body.split())
    if body:
        try:
            payload = json.loads(body)
            if isinstance(payload, dict) and payload.get("message"):
                body = str(payload["message"])
        except json.JSONDecodeError:
            pass
        details.append(body[:200])
    suffix = f" ({'; '.join(details)})" if details else ""
    return f"GitHub returned HTTP {error.code} for {endpoint}{suffix}"


def _request_bytes(
    url: str,
    timeout: float = 15.0,
    *,
    accept: str | None = None,
) -> bytes:
    global _api_cooldown_until
    is_api = _is_github_api_url(url)
    now = time.time()
    if is_api and now < _api_cooldown_until:
        wait_seconds = max(1, int(_api_cooldown_until - now))
        raise UpdateTransportError(
            f"GitHub API fallback is cooling down for {wait_seconds}s"
        )
    headers = {
        "Accept": accept or (
            "application/vnd.github+json"
            if is_api
            else "application/octet-stream"
        ),
        "Cache-Control": "no-cache",
        "User-Agent": USER_AGENT,
    }
    if is_api:
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    for attempt in range(HTTP_RETRY_ATTEMPTS):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                value = response.read()
                if is_api:
                    _api_cooldown_until = 0.0
                return value
        except HTTPError as exc:
            if exc.code == 404:
                raise NoPublishedRelease(
                    "No published GitHub release is available"
                ) from exc
            _record_api_cooldown(url, exc)
            delay = _http_retry_delay(exc, attempt)
            if delay is not None and attempt + 1 < HTTP_RETRY_ATTEMPTS:
                if delay > 0:
                    time.sleep(delay)
                continue
            raise UpdateTransportError(
                _http_error_message(exc, url)
            ) from exc
        except (OSError, URLError, TimeoutError) as exc:
            raise UpdateTransportError(
                f"Could not reach GitHub: {exc}"
            ) from exc
    raise UpdateTransportError("Could not reach GitHub after retrying")


def _request_json(
    url: str,
    timeout: float = 15.0,
    *,
    accept: str | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(_request_bytes(url, timeout, accept=accept))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpdateTransportError("GitHub returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise UpdateTransportError("GitHub returned an unexpected response")
    return value


def _asset_map(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for asset in release.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name"):
            result[str(asset["name"])] = asset
    return result


def _validate_manifest_header(manifest: dict[str, Any]) -> None:
    schema = manifest.get("schema")
    if type(schema) is not int or schema != 1:
        raise UpdateError("Unsupported update.json schema")
    if str(manifest.get("channel") or "") != "stable":
        raise UpdateError("The latest release is not on the stable channel")
    if str(manifest.get("repository") or "") != (
        release_config.RELEASE_REPOSITORY
    ):
        raise UpdateError("update.json targets a different repository")


def _release_download_url(tag: str, name: str) -> str:
    return (
        f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
        f"{quote(tag, safe='')}/{quote(name, safe='')}"
    )


def _manifest_size(value: Any, name: str) -> int:
    if value in (None, ""):
        return 0
    if type(value) is not int or value < 0:
        raise UpdateError(f"{name} has an invalid download size")
    return value


def _manifest_version(value: Any, label: str) -> str:
    version = str(value or "")
    if not RELEASE_VERSION.fullmatch(version):
        raise UpdateError(f"{label} has an invalid version")
    return version


def _manifest_percentage(value: Any, label: str) -> int:
    if type(value) is not int or not 0 <= value <= 100:
        raise UpdateError(f"{label} has an invalid percentage")
    return value


def _manifest_asset(
    assets: dict[str, dict[str, Any]],
    name: str,
    expected_sha: Any,
    expected_size: Any,
    tag: str,
) -> ReleaseAsset:
    source = assets.get(name)
    if not source:
        raise UpdateError(f"Release asset is missing: {name}")
    manifest_sha = _sha256_text(expected_sha, name)
    github_digest = str(source.get("digest") or "").lower()
    if github_digest:
        github_sha = _sha256_text(github_digest, name)
        if github_sha != manifest_sha:
            raise UpdateError(f"GitHub and update.json hashes disagree for {name}")
    manifest_size = _manifest_size(expected_size, name)
    github_size = _manifest_size(source.get("size"), name)
    if manifest_size and github_size and manifest_size != github_size:
        raise UpdateError(
            f"GitHub and update.json sizes disagree for {name}"
        )
    return ReleaseAsset(
        name=name,
        url=_release_download_url(tag, name),
        size=manifest_size or github_size,
        sha256=manifest_sha,
    )


def parse_release(
    release: dict[str, Any], manifest: dict[str, Any]
) -> UpdateRelease:
    _validate_manifest_header(manifest)
    assets = _asset_map(release)
    client = manifest.get("client") or {}
    firmware = manifest.get("firmware") or {}
    if not isinstance(client, dict) or not isinstance(firmware, dict):
        raise UpdateError("update.json is missing client or firmware metadata")

    client_asset_name = str(
        client.get("asset") or release_config.CLIENT_ASSET_NAME
    )
    firmware_asset_name = str(
        firmware.get("asset") or release_config.FIRMWARE_OTA_ASSET_NAME
    )
    if client_asset_name != release_config.CLIENT_ASSET_NAME:
        raise UpdateError("update.json names an unexpected client asset")
    if firmware_asset_name != release_config.FIRMWARE_OTA_ASSET_NAME:
        raise UpdateError("update.json names an unexpected firmware asset")
    client_version = _manifest_version(
        client.get("version"), "Client release"
    )
    firmware_version = _manifest_version(
        firmware.get("version"), "Firmware release"
    )
    min_client = _manifest_version(
        firmware.get("min_client") or "0.0.0",
        "Minimum client release",
    )
    min_battery = _manifest_percentage(
        firmware.get("min_battery", 30), "Minimum battery"
    )
    board = str(firmware.get("board") or "")
    if board != "waveshare-amoled-2.16":
        raise UpdateError("Firmware release targets a different board")
    release_tag = str(release.get("tag_name") or "")
    manifest_tag = str(manifest.get("tag") or "")
    tag = manifest_tag or release_tag
    if not RELEASE_TAG.fullmatch(tag):
        raise UpdateError("update.json has an invalid release tag")
    if manifest_tag and release_tag and manifest_tag != release_tag:
        raise UpdateError("GitHub and update.json release tags disagree")
    client_release = ClientRelease(
        version=client_version,
        asset=_manifest_asset(
            assets,
            client_asset_name,
            client.get("sha256"),
            client.get("size"),
            tag,
        ),
    )
    firmware_release = FirmwareRelease(
        version=firmware_version,
        asset=_manifest_asset(
            assets,
            firmware_asset_name,
            firmware.get("sha256"),
            firmware.get("size"),
            tag,
        ),
        board=board,
        min_client=min_client,
        min_battery=min_battery,
    )
    return UpdateRelease(
        tag=tag,
        client=client_release,
        firmware=firmware_release,
    )


def _release_from_manifest(manifest: dict[str, Any]) -> UpdateRelease:
    _validate_manifest_header(manifest)
    client = manifest.get("client") or {}
    firmware = manifest.get("firmware") or {}
    if not isinstance(client, dict) or not isinstance(firmware, dict):
        raise UpdateError("update.json is missing client or firmware metadata")
    client_version = str(client.get("version") or "")
    firmware_version = str(firmware.get("version") or "")
    tag = str(manifest.get("tag") or "")
    if not tag:
        if not client_version or client_version != firmware_version:
            raise UpdateError(
                "Legacy update.json requires matching component versions"
            )
        tag = f"v{client_version}"
    client_name = str(
        client.get("asset") or release_config.CLIENT_ASSET_NAME
    )
    firmware_name = str(
        firmware.get("asset") or release_config.FIRMWARE_OTA_ASSET_NAME
    )
    release = {
        "tag_name": tag,
        "assets": [
            {
                "name": client_name,
                "size": _manifest_size(
                    client.get("size"), client_name
                ),
            },
            {
                "name": firmware_name,
                "size": _manifest_size(
                    firmware.get("size"), firmware_name
                ),
            },
        ],
    }
    return parse_release(release, manifest)


def _fetch_latest_release_via_api(timeout: float) -> UpdateRelease:
    release = _request_json(release_config.RELEASE_API_URL, timeout)
    if release.get("draft") or release.get("prerelease"):
        raise NoPublishedRelease("No stable GitHub release is available")
    release_tag = str(release.get("tag_name") or "")
    if not RELEASE_TAG.fullmatch(release_tag):
        raise UpdateError("GitHub release has an invalid tag")
    assets = _asset_map(release)
    manifest_asset = assets.get(release_config.MANIFEST_ASSET_NAME)
    if not manifest_asset:
        raise UpdateError(
            f"Latest release is missing {release_config.MANIFEST_ASSET_NAME}"
        )
    manifest_url = _release_download_url(
        release_tag, release_config.MANIFEST_ASSET_NAME
    )
    manifest = _request_json(manifest_url, timeout)
    return parse_release(release, manifest)


def fetch_latest_release(timeout: float = 15.0) -> UpdateRelease:
    try:
        manifest = _request_json(
            release_config.LATEST_MANIFEST_URL,
            timeout,
            accept="application/json",
        )
    except (NoPublishedRelease, UpdateTransportError) as direct_error:
        try:
            return _fetch_latest_release_via_api(timeout)
        except NoPublishedRelease:
            if isinstance(direct_error, NoPublishedRelease):
                raise
            raise UpdateError(
                f"Direct update check failed: {direct_error}; "
                "GitHub API reports no stable release"
            ) from direct_error
        except UpdateError as api_error:
            raise UpdateError(
                f"Direct update check failed: {direct_error}; "
                f"API fallback failed: {api_error}"
            ) from api_error
    return _release_from_manifest(manifest)


def download_asset(
    asset: ReleaseAsset,
    destination: Path,
    progress: Callable[[int], None] | None = None,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(asset.url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    received = 0
    last_percent = -1
    try:
        with urlopen(request, timeout=30.0) as response, temporary.open("wb") as output:
            total = asset.size or int(response.headers.get("Content-Length") or 0)
            while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                output.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                percent = min(100, received * 100 // total) if total else 0
                if progress and percent != last_percent:
                    last_percent = percent
                    progress(percent)
    except HTTPError as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(
            f"Download failed for {asset.name}: "
            f"{_http_error_message(exc, asset.url)}"
        ) from exc
    except (OSError, URLError, TimeoutError) as exc:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"Download failed for {asset.name}: {exc}") from exc
    if asset.size and received != asset.size:
        temporary.unlink(missing_ok=True)
        raise UpdateError(
            f"Download size mismatch for {asset.name}: {received}/{asset.size}"
        )
    if digest.hexdigest() != asset.sha256:
        temporary.unlink(missing_ok=True)
        raise UpdateError(f"SHA-256 verification failed for {asset.name}")
    temporary.replace(destination)
    if progress:
        progress(100)
    return destination


def launch_integrated_client_updater(
    launcher: Path, new_client: Path, target: Path, expected_sha256: str
) -> None:
    if not launcher.is_file() or not new_client.is_file():
        raise UpdateError("Client update files are unavailable")
    if not target.is_absolute():
        target = target.resolve()
    update_host = new_client.parent / "CodexMeterSync.UpdateHost.exe"
    shutil.copy2(launcher, update_host)
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [
            str(update_host),
            "--integrated-update",
            "--wait-pid", str(os.getpid()),
            "--source", str(new_client),
            "--target", str(target),
            "--sha256", expected_sha256,
            "--restart",
        ],
        cwd=str(update_host.parent),
        close_fds=True,
        creationflags=creation_flags,
    )


def _client_has_characteristic(client, uuid: str) -> bool:
    try:
        services = client.services
        return services.get_characteristic(uuid) is not None
    except (AttributeError, KeyError, RuntimeError):
        return False


def _client_has_ota_transport(client) -> bool:
    return all(
        _client_has_characteristic(client, uuid)
        for uuid in (
            OTA_CONTROL_CHARACTERISTIC_UUID,
            OTA_DATA_CHARACTERISTIC_UUID,
            OTA_STATUS_CHARACTERISTIC_UUID,
        )
    )


async def _probe_ota_transport(client) -> bool:
    if _client_has_ota_transport(client):
        return True
    try:
        raw = await client.read_gatt_char(OTA_STATUS_CHARACTERISTIC_UUID)
        return bool(bytes(raw))
    except Exception:
        return False


async def read_device_info(
    client, attempts: int = 3, retry_delay: float = 0.25
) -> dict[str, Any]:
    last_error = ""
    for attempt in range(max(1, attempts)):
        try:
            raw = await client.read_gatt_char(
                DEVICE_INFO_CHARACTERISTIC_UUID
            )
            info = json.loads(bytes(raw).decode("utf-8"))
            if not isinstance(info, dict):
                raise ValueError("device info is not an object")
            return {
                "version": str(info.get("version") or ""),
                "protocol": int(info.get("protocol") or 0),
                "ota": bool(info.get("ota")),
                "board": str(info.get("board") or ""),
                "slot": str(info.get("slot") or ""),
                "legacy": False,
            }
        except Exception as exc:
            last_error = str(exc) or type(exc).__name__
            if attempt + 1 < max(1, attempts) and retry_delay > 0:
                await asyncio.sleep(retry_delay * (attempt + 1))

    # Some Windows Bluetooth stacks can transiently reject a characteristic
    # read even though service discovery succeeded. If the complete OTA
    # transport is visible, keep firmware updates available and let the device
    # validate the target version when the transfer begins.
    ota_transport = await _probe_ota_transport(client)
    return {
        "version": "",
        "protocol": 0,
        "ota": ota_transport,
        "board": "",
        "slot": "",
        "legacy": not ota_transport,
        "error": last_error,
    }


async def send_device_update_state(
    client, request: DeviceUpdateDisplayRequest
) -> None:
    state = str(request.state or "IDLE").strip().upper()
    if state not in DEVICE_UPDATE_STATES:
        raise UpdateError(f"Unsupported device update display state: {state}")
    payload = json.dumps(
        {
            "op": "display",
            "state": state,
            "progress": max(0, min(100, int(request.progress))),
        },
        separators=(",", ":"),
    ).encode("utf-8")
    await client.write_gatt_char(
        OTA_CONTROL_CHARACTERISTIC_UUID, payload, response=True
    )


async def _next_ota_status(
    queue: asyncio.Queue[dict[str, Any]],
    accepted: set[str],
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError("Device OTA status timeout")
        status = await asyncio.wait_for(queue.get(), remaining)
        state = str(status.get("state") or "")
        if state == "error":
            raise UpdateError(str(status.get("error") or "Device rejected OTA"))
        if state in accepted:
            return status


async def _wait_for_ota_offset(
    queue: asyncio.Queue[dict[str, Any]],
    expected_offset: int,
    timeout: float,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise TimeoutError(
                f"Device did not confirm all firmware bytes "
                f"({expected_offset})"
            )
        status = await asyncio.wait_for(queue.get(), remaining)
        state = str(status.get("state") or "")
        if state == "error":
            raise UpdateError(
                str(status.get("error") or "Device rejected OTA")
            )
        try:
            offset = int(status.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset >= expected_offset:
            return status


async def perform_ble_firmware_update(
    client,
    request: FirmwareUpdateRequest,
    status_callback: Callable[[str], None] | None = None,
) -> None:
    if file_sha256(request.path) != request.sha256:
        raise UpdateError("Local firmware SHA-256 verification failed")
    status_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_status(_, value: bytearray) -> None:
        try:
            parsed = json.loads(bytes(value).decode("utf-8"))
            if isinstance(parsed, dict):
                loop.call_soon_threadsafe(status_queue.put_nowait, parsed)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return

    await client.start_notify(OTA_STATUS_CHARACTERISTIC_UUID, on_status)
    try:
        begin = json.dumps(
            {
                "op": "begin",
                "size": request.size,
                "sha256": request.sha256,
                "version": request.version,
                "board": "waveshare-amoled-2.16",
            },
            separators=(",", ":"),
        ).encode("utf-8")
        await client.write_gatt_char(
            OTA_CONTROL_CHARACTERISTIC_UUID, begin, response=True
        )
        await _next_ota_status(status_queue, {"ready"}, 15.0)
        if status_callback:
            status_callback(f"Firmware update started: {request.version}")

        offset = 0
        last_reported = -2
        confirmed_offset = 0
        with request.path.open("rb") as stream:
            while chunk := stream.read(BLE_FIRMWARE_CHUNK_SIZE):
                payload = struct.pack("<I", offset) + chunk
                # Windows can submit write-without-response packets faster
                # than the controller and ESP32 consume them. One dropped
                # packet invalidates every following offset, so acknowledge
                # every firmware chunk. OTA is infrequent and reliability is
                # more important than the small burst-throughput gain.
                await client.write_gatt_char(
                    OTA_DATA_CHARACTERISTIC_UUID,
                    payload,
                    response=True,
                )
                offset += len(chunk)
                percent = min(100, offset * 100 // request.size)
                if percent >= last_reported + 2 or percent == 100:
                    # The ESP32 publishes its applied offset at the same
                    # two-percent cadence. Waiting here provides application-
                    # level flow control in addition to the GATT response, and
                    # surfaces an offset error immediately instead of letting
                    # the local progress counter continue to a misleading 100%.
                    status = await _wait_for_ota_offset(
                        status_queue, offset, 15.0
                    )
                    confirmed_offset = max(
                        confirmed_offset,
                        int(status.get("offset") or 0),
                    )
                    last_reported = percent
                    if status_callback:
                        status_callback(f"Updating device firmware: {percent}%")

        # A local 100% counter only means all writes returned successfully.
        # Also require the ESP32's own final offset before finalizing.
        if confirmed_offset < request.size:
            await _wait_for_ota_offset(status_queue, request.size, 20.0)
        finish = json.dumps({"op": "finish"}, separators=(",", ":")).encode()
        try:
            await client.write_gatt_char(
                OTA_CONTROL_CHARACTERISTIC_UUID, finish, response=True
            )
            await _next_ota_status(
                status_queue, {"complete", "restarting"}, 30.0
            )
        except Exception:
            if getattr(client, "is_connected", True):
                raise
            if status_callback:
                status_callback(
                    f"Firmware {request.version} transferred; "
                    "reconnecting to verify"
                )
            return
        if status_callback:
            status_callback(
                f"Firmware {request.version} transferred; device restarting"
            )
    except Exception:
        with suppress(Exception):
            abort = json.dumps({"op": "abort"}, separators=(",", ":")).encode()
            await client.write_gatt_char(
                OTA_CONTROL_CHARACTERISTIC_UUID, abort, response=True
            )
        raise
    finally:
        with suppress(Exception):
            await client.stop_notify(OTA_STATUS_CHARACTERISTIC_UUID)
