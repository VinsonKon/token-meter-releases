from __future__ import annotations

import asyncio
from io import BytesIO
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from urllib.error import HTTPError

import release_config
import update_manager


CLIENT_SHA = "1" * 64
FIRMWARE_SHA = "2" * 64


class FakeResponse:
    def __init__(
        self, body: bytes, headers: dict[str, str] | None = None
    ) -> None:
        self._body = BytesIO(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def manifest(*, tag: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": 1,
        "channel": "stable",
        "repository": release_config.RELEASE_REPOSITORY,
        "client": {
            "version": "1.0.2",
            "asset": release_config.CLIENT_ASSET_NAME,
            "size": 123,
            "sha256": CLIENT_SHA,
        },
        "firmware": {
            "version": "1.0.2",
            "board": "waveshare-amoled-2.16",
            "min_client": "1.0.2",
            "min_battery": 30,
            "asset": release_config.FIRMWARE_OTA_ASSET_NAME,
            "size": 456,
            "sha256": FIRMWARE_SHA,
        },
    }
    if tag:
        value["tag"] = "v1.0.2"
    return value


def response_json(value: dict[str, object]) -> FakeResponse:
    return FakeResponse(json.dumps(value).encode("utf-8"))


def http_error(
    url: str,
    status: int,
    *,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> HTTPError:
    return HTTPError(
        url,
        status,
        "test error",
        headers or {},
        BytesIO(body),
    )


class UpdateManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        update_manager._api_cooldown_until = 0.0

    def test_direct_manifest_success_never_calls_api(self) -> None:
        with mock.patch.object(
            update_manager, "urlopen", return_value=response_json(manifest())
        ) as mocked:
            release = update_manager.fetch_latest_release()

        self.assertEqual(1, mocked.call_count)
        request = mocked.call_args.args[0]
        self.assertEqual(
            release_config.LATEST_MANIFEST_URL, request.full_url
        )
        self.assertEqual("v1.0.2", release.tag)
        self.assertEqual(
            (
                f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
                "v1.0.2/CodexMeterSync.exe"
            ),
            release.client.asset.url,
        )
        self.assertEqual(
            (
                f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
                "v1.0.2/codex-token-monitor.ota.bin"
            ),
            release.firmware.asset.url,
        )

    def test_legacy_manifest_derives_tag_and_allows_unknown_sizes(self) -> None:
        legacy = manifest(tag=False)
        legacy["client"].pop("size")
        legacy["firmware"].pop("size")
        with mock.patch.object(
            update_manager, "urlopen", return_value=response_json(legacy)
        ):
            release = update_manager.fetch_latest_release()

        self.assertEqual("v1.0.2", release.tag)
        self.assertEqual(0, release.client.asset.size)
        self.assertEqual(0, release.firmware.asset.size)

    def test_direct_403_falls_back_to_release_api(self) -> None:
        manifest_url = (
            f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
            "v1.0.2/update.json"
        )
        api_release = {
            "tag_name": "v1.0.2",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": release_config.MANIFEST_ASSET_NAME,
                    "browser_download_url": manifest_url,
                },
                {
                    "name": release_config.CLIENT_ASSET_NAME,
                    "browser_download_url": (
                        f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/"
                        "download/v1.0.2/CodexMeterSync.exe"
                    ),
                    "size": 123,
                    "digest": f"sha256:{CLIENT_SHA}",
                },
                {
                    "name": release_config.FIRMWARE_OTA_ASSET_NAME,
                    "browser_download_url": (
                        f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/"
                        "download/v1.0.2/codex-token-monitor.ota.bin"
                    ),
                    "size": 456,
                    "digest": f"sha256:{FIRMWARE_SHA}",
                },
            ],
        }
        calls: list[str] = []

        def open_url(request, timeout):
            calls.append(request.full_url)
            if request.full_url == release_config.LATEST_MANIFEST_URL:
                raise http_error(
                    request.full_url,
                    403,
                    body=b"blocked by proxy",
                )
            if request.full_url == release_config.RELEASE_API_URL:
                return response_json(api_release)
            if request.full_url == manifest_url:
                return response_json(manifest())
            raise AssertionError(f"Unexpected request: {request.full_url}")

        with mock.patch.object(update_manager, "urlopen", side_effect=open_url):
            release = update_manager.fetch_latest_release()

        self.assertEqual("v1.0.2", release.tag)
        self.assertEqual(
            [
                release_config.LATEST_MANIFEST_URL,
                release_config.RELEASE_API_URL,
                manifest_url,
            ],
            calls,
        )

    def test_manifest_validation_failure_does_not_call_api(self) -> None:
        invalid = manifest()
        invalid["repository"] = "someone/other-repository"
        with mock.patch.object(
            update_manager, "urlopen", return_value=response_json(invalid)
        ) as mocked:
            with self.assertRaisesRegex(
                update_manager.UpdateError, "different repository"
            ):
                update_manager.fetch_latest_release()

        self.assertEqual(1, mocked.call_count)

    def test_manifest_rejects_unexpected_asset_and_invalid_tag(self) -> None:
        invalid_asset = manifest()
        invalid_asset["client"]["asset"] = "Different.exe"
        with mock.patch.object(
            update_manager,
            "urlopen",
            return_value=response_json(invalid_asset),
        ):
            with self.assertRaisesRegex(
                update_manager.UpdateError, "unexpected client asset"
            ):
                update_manager.fetch_latest_release()

        invalid_tag = manifest()
        invalid_tag["tag"] = "latest"
        with mock.patch.object(
            update_manager,
            "urlopen",
            return_value=response_json(invalid_tag),
        ):
            with self.assertRaisesRegex(
                update_manager.UpdateError, "invalid release tag"
            ):
                update_manager.fetch_latest_release()

    def test_explicit_tag_allows_independent_component_versions(self) -> None:
        independent = manifest()
        independent["tag"] = "v1.0.3"
        independent["client"]["version"] = "1.0.3"
        independent["firmware"]["version"] = "1.0.2"
        with mock.patch.object(
            update_manager,
            "urlopen",
            return_value=response_json(independent),
        ):
            release = update_manager.fetch_latest_release()

        self.assertEqual("v1.0.3", release.tag)
        self.assertEqual("1.0.3", release.client.version)
        self.assertEqual("1.0.2", release.firmware.version)
        self.assertIn("/download/v1.0.3/", release.firmware.asset.url)

    def test_manifest_rejects_non_integer_size_and_invalid_version(self) -> None:
        invalid_size = manifest()
        invalid_size["client"]["size"] = 1.5
        with mock.patch.object(
            update_manager,
            "urlopen",
            return_value=response_json(invalid_size),
        ):
            with self.assertRaisesRegex(
                update_manager.UpdateError, "invalid download size"
            ):
                update_manager.fetch_latest_release()

        invalid_version = manifest()
        invalid_version["client"]["version"] = "vv1.0.2"
        with mock.patch.object(
            update_manager,
            "urlopen",
            return_value=response_json(invalid_version),
        ):
            with self.assertRaisesRegex(
                update_manager.UpdateError, "invalid version"
            ):
                update_manager.fetch_latest_release()

    def test_api_403_reports_rate_limit_and_enters_cooldown(self) -> None:
        reset = int(time.time()) + 3600
        calls: list[str] = []

        def open_url(request, timeout):
            calls.append(request.full_url)
            if request.full_url == release_config.LATEST_MANIFEST_URL:
                raise http_error(
                    request.full_url,
                    403,
                    body=b"blocked by proxy",
                )
            raise http_error(
                request.full_url,
                403,
                headers={
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset),
                    "X-GitHub-Request-Id": "ABC:123",
                },
                body=b'{"message":"API rate limit exceeded"}',
            )

        with mock.patch.object(update_manager, "urlopen", side_effect=open_url):
            with self.assertRaises(update_manager.UpdateError) as caught:
                update_manager.fetch_latest_release()

        message = str(caught.exception)
        self.assertIn("rate remaining 0", message)
        self.assertIn("API rate limit exceeded", message)
        self.assertIn("request ABC:123", message)
        self.assertGreaterEqual(update_manager._api_cooldown_until, reset)
        self.assertEqual(
            [
                release_config.LATEST_MANIFEST_URL,
                release_config.RELEASE_API_URL,
            ],
            calls,
        )

    def test_api_cooldown_never_blocks_recovered_direct_path(self) -> None:
        update_manager._api_cooldown_until = time.time() + 3600
        with mock.patch.object(
            update_manager, "urlopen", return_value=response_json(manifest())
        ) as mocked:
            release = update_manager.fetch_latest_release()

        self.assertEqual("v1.0.2", release.tag)
        self.assertEqual(1, mocked.call_count)

    def test_api_429_without_retry_after_does_not_retry(self) -> None:
        error = http_error(release_config.RELEASE_API_URL, 429)
        with mock.patch.object(
            update_manager, "urlopen", side_effect=error
        ) as mocked, mock.patch.object(update_manager.time, "sleep") as sleep:
            with self.assertRaises(update_manager.UpdateTransportError):
                update_manager._request_bytes(
                    release_config.RELEASE_API_URL
                )

        self.assertEqual(1, mocked.call_count)
        sleep.assert_not_called()
        self.assertGreater(
            update_manager._api_cooldown_until, time.time()
        )

    def test_api_cooldown_skips_api_when_direct_still_fails(self) -> None:
        update_manager._api_cooldown_until = time.time() + 3600

        def open_url(request, timeout):
            if request.full_url != release_config.LATEST_MANIFEST_URL:
                raise AssertionError("API should be skipped during cooldown")
            raise http_error(request.full_url, 403)

        with mock.patch.object(
            update_manager, "urlopen", side_effect=open_url
        ) as mocked:
            with self.assertRaisesRegex(
                update_manager.UpdateError, "cooling down"
            ):
                update_manager.fetch_latest_release()

        self.assertEqual(1, mocked.call_count)

    def test_invalid_utf8_direct_response_reaches_api_fallback(self) -> None:
        api_release = {
            "tag_name": "v1.0.2",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": release_config.MANIFEST_ASSET_NAME,
                },
                {
                    "name": release_config.CLIENT_ASSET_NAME,
                    "size": 123,
                    "digest": f"sha256:{CLIENT_SHA}",
                },
                {
                    "name": release_config.FIRMWARE_OTA_ASSET_NAME,
                    "size": 456,
                    "digest": f"sha256:{FIRMWARE_SHA}",
                },
            ],
        }
        manifest_url = (
            f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
            "v1.0.2/update.json"
        )

        def open_url(request, timeout):
            if request.full_url == release_config.LATEST_MANIFEST_URL:
                return FakeResponse(b"\xff")
            if request.full_url == release_config.RELEASE_API_URL:
                return response_json(api_release)
            if request.full_url == manifest_url:
                return response_json(manifest())
            raise AssertionError(f"Unexpected request: {request.full_url}")

        with mock.patch.object(update_manager, "urlopen", side_effect=open_url):
            release = update_manager.fetch_latest_release()

        self.assertEqual("v1.0.2", release.tag)

    def test_unknown_size_download_uses_hash_and_finishes_at_100(self) -> None:
        data = b"release asset without a manifest size"
        asset = update_manager.ReleaseAsset(
            name="asset.bin",
            url=(
                f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
                "v1.0.2/asset.bin"
            ),
            size=0,
            sha256=hashlib.sha256(data).hexdigest(),
        )
        progress: list[int] = []
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / asset.name
            with mock.patch.object(
                update_manager,
                "urlopen",
                return_value=FakeResponse(data),
            ):
                update_manager.download_asset(
                    asset, destination, progress.append
                )
            self.assertEqual(data, destination.read_bytes())

        self.assertEqual(100, progress[-1])

    def test_hash_mismatch_removes_part_and_preserves_target(self) -> None:
        data = b"new but corrupted release asset"
        asset = update_manager.ReleaseAsset(
            name="asset.bin",
            url=(
                f"{release_config.RELEASE_DOWNLOAD_BASE_URL}/download/"
                "v1.0.2/asset.bin"
            ),
            size=len(data),
            sha256="0" * 64,
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / asset.name
            destination.write_bytes(b"existing valid installation")
            with mock.patch.object(
                update_manager,
                "urlopen",
                return_value=FakeResponse(data),
            ):
                with self.assertRaisesRegex(
                    update_manager.UpdateError, "SHA-256 verification failed"
                ):
                    update_manager.download_asset(asset, destination)

            self.assertEqual(
                b"existing valid installation", destination.read_bytes()
            )
            self.assertFalse(
                destination.with_suffix(".bin.part").exists()
            )


class FakeOtaClient:
    def __init__(self, *, disconnect_on_finish: bool = False) -> None:
        self.disconnect_on_finish = disconnect_on_finish
        self.is_connected = True
        self.status_handler = None
        self.expected_size = 0
        self.offset = 0
        self.data_responses: list[bool] = []
        self.finish_saw_complete_image = False

    async def start_notify(self, _uuid, handler) -> None:
        self.status_handler = handler

    async def stop_notify(self, _uuid) -> None:
        return None

    def notify(self, payload: dict[str, object]) -> None:
        assert self.status_handler is not None
        self.status_handler(
            None,
            bytearray(json.dumps(payload).encode("utf-8")),
        )

    async def write_gatt_char(
        self, uuid: str, payload: bytes, *, response: bool
    ) -> None:
        if uuid == update_manager.OTA_CONTROL_CHARACTERISTIC_UUID:
            operation = json.loads(bytes(payload).decode("utf-8"))
            if operation["op"] == "begin":
                self.expected_size = int(operation["size"])
                self.notify({
                    "state": "ready",
                    "offset": 0,
                    "size": self.expected_size,
                })
                return
            if operation["op"] == "finish":
                self.finish_saw_complete_image = (
                    self.offset == self.expected_size
                )
                if self.disconnect_on_finish:
                    self.is_connected = False
                    raise ConnectionError("device rebooted")
                self.notify({
                    "state": "restarting",
                    "offset": self.offset,
                    "size": self.expected_size,
                })
                return
            return

        if uuid == update_manager.OTA_DATA_CHARACTERISTIC_UUID:
            offset = int.from_bytes(payload[:4], "little")
            if offset != self.offset:
                raise AssertionError(
                    f"unexpected OTA offset {offset}, expected {self.offset}"
                )
            self.data_responses.append(response)
            self.offset += len(payload) - 4
            self.notify({
                "state": "progress",
                "offset": self.offset,
                "size": self.expected_size,
            })


class BleFirmwareUpdateTests(unittest.TestCase):
    def _run_update(
        self, client: FakeOtaClient
    ) -> tuple[list[str], list[bool]]:
        data = bytes(range(250)) * 8
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "firmware.bin"
            path.write_bytes(data)
            request = update_manager.FirmwareUpdateRequest(
                path=path,
                version="1.0.2",
                sha256=hashlib.sha256(data).hexdigest(),
                size=len(data),
            )
            statuses: list[str] = []
            asyncio.run(
                update_manager.perform_ble_firmware_update(
                    client, request, statuses.append
                )
            )
        return statuses, client.data_responses

    def test_every_chunk_is_acknowledged_and_final_offset_is_verified(
        self,
    ) -> None:
        client = FakeOtaClient()

        statuses, responses = self._run_update(client)

        self.assertTrue(client.finish_saw_complete_image)
        self.assertGreater(len(responses), 8)
        self.assertTrue(all(responses))
        self.assertEqual(
            "Firmware 1.0.2 transferred; device restarting",
            statuses[-1],
        )
        self.assertFalse(any("installed" in value for value in statuses))

    def test_disconnect_after_finish_waits_for_version_verification(self) -> None:
        client = FakeOtaClient(disconnect_on_finish=True)

        statuses, _responses = self._run_update(client)

        self.assertTrue(client.finish_saw_complete_image)
        self.assertEqual(
            "Firmware 1.0.2 transferred; reconnecting to verify",
            statuses[-1],
        )
        self.assertFalse(any("installed" in value for value in statuses))


if __name__ == "__main__":
    unittest.main()
