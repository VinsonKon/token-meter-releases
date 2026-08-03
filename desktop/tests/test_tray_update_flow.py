from __future__ import annotations

from pathlib import Path
import json
import queue
import tempfile
import threading
import unittest
from unittest import mock

import tray_app
import update_manager


def release() -> update_manager.UpdateRelease:
    client_asset = update_manager.ReleaseAsset(
        name="CodexMeterSync.exe",
        url="https://github.com/example/client",
        size=100,
        sha256="1" * 64,
    )
    firmware_asset = update_manager.ReleaseAsset(
        name="codex-token-monitor.ota.bin",
        url="https://github.com/example/firmware",
        size=200,
        sha256="2" * 64,
    )
    return update_manager.UpdateRelease(
        tag="v1.0.2",
        client=update_manager.ClientRelease(
            version="1.0.2", asset=client_asset
        ),
        firmware=update_manager.FirmwareRelease(
            version="1.0.2",
            asset=firmware_asset,
            board="waveshare-amoled-2.16",
            min_client="1.0.2",
            min_battery=30,
        ),
    )


def service_for_confirmation():
    service = tray_app.TrayService.__new__(tray_app.TrayService)
    service.update_lock = threading.Lock()
    service.update_release = release()
    service.device_info = {"ota": True, "version": "1.0.1"}
    service.firmware_update_in_progress = False
    service.firmware_update_phase = tray_app.FIRMWARE_PHASE_IDLE
    service.firmware_update_queue = queue.Queue(maxsize=1)
    service.app_title = "Codex Meter Sync"
    service._queue_device_update_state = mock.Mock()
    service._set_status = mock.Mock()
    service._notify_user = mock.Mock()
    service._update_device_menu = mock.Mock()
    return service


class FirmwareConfirmationTests(unittest.TestCase):
    def test_menu_callback_schedules_confirmation_and_returns(self) -> None:
        service = service_for_confirmation()
        worker = mock.Mock()
        with mock.patch.object(
            tray_app.threading, "Thread", return_value=worker
        ) as thread, mock.patch.object(
            tray_app, "confirm_dialog"
        ) as confirm:
            service._install_firmware_update(None, None)

        self.assertTrue(service.firmware_update_in_progress)
        service._set_status.assert_called_once_with(
            "Waiting for firmware update confirmation"
        )
        service._update_device_menu.assert_called_once_with()
        confirm.assert_not_called()
        thread.assert_called_once_with(
            target=service._confirm_firmware_update_worker,
            args=(service.update_release,),
            daemon=True,
        )
        worker.start.assert_called_once_with()

    def test_second_click_does_not_schedule_another_confirmation(self) -> None:
        service = service_for_confirmation()
        service.firmware_update_in_progress = True
        with mock.patch.object(tray_app.threading, "Thread") as thread:
            service._install_firmware_update(None, None)

        thread.assert_not_called()
        service._set_status.assert_not_called()

    def test_yes_starts_download_from_confirmation_worker(self) -> None:
        service = service_for_confirmation()
        service.firmware_update_in_progress = True
        service._install_firmware_update_worker = mock.Mock()
        with mock.patch.object(
            tray_app, "confirm_dialog", return_value=True
        ):
            service._confirm_firmware_update_worker(
                service.update_release
            )

        self.assertTrue(service.firmware_update_in_progress)
        service._queue_device_update_state.assert_called_once_with(
            "DOWNLOADING", 0
        )
        service._set_status.assert_called_once_with(
            "Firmware update 1.0.2 confirmed; starting download"
        )
        service._notify_user.assert_called_once_with(
            "Downloading device firmware 1.0.2..."
        )
        service._install_firmware_update_worker.assert_called_once_with(
            service.update_release
        )

    def test_no_reports_cancellation_and_unlocks_menu(self) -> None:
        service = service_for_confirmation()
        service.firmware_update_in_progress = True
        service._install_firmware_update_worker = mock.Mock()
        with mock.patch.object(
            tray_app, "confirm_dialog", return_value=False
        ):
            service._confirm_firmware_update_worker(
                service.update_release
            )

        self.assertFalse(service.firmware_update_in_progress)
        service._set_status.assert_called_once_with(
            "Device firmware update cancelled"
        )
        service._update_device_menu.assert_called_once_with()
        service._install_firmware_update_worker.assert_not_called()

    def test_thread_start_failure_is_visible_and_unlocks_menu(self) -> None:
        service = service_for_confirmation()
        with mock.patch.object(
            tray_app.threading,
            "Thread",
            side_effect=RuntimeError("thread unavailable"),
        ), mock.patch.object(tray_app, "error_dialog") as dialog:
            service._install_firmware_update(None, None)

        self.assertFalse(service.firmware_update_in_progress)
        service._set_status.assert_called_with(
            "Could not start firmware update confirmation: "
            "thread unavailable"
        )
        service._queue_device_update_state.assert_not_called()
        dialog.assert_called_once()
        self.assertEqual(service._update_device_menu.call_count, 2)

    def test_download_failure_is_visible_and_unlocks_menu(self) -> None:
        service = service_for_confirmation()
        with tempfile.TemporaryDirectory() as directory:
            service.state_dir = Path(directory)
            service.pending_firmware_path = (
                service.state_dir / ".pending-firmware-update.json"
            )
            service.pending_firmware_path.write_text(
                "pending", encoding="utf-8"
            )
            with mock.patch.object(
                update_manager,
                "download_asset",
                side_effect=update_manager.UpdateError("network blocked"),
            ), mock.patch.object(tray_app, "error_dialog") as dialog:
                service.firmware_update_in_progress = True
                service._install_firmware_update_worker(
                    service.update_release
                )

            self.assertFalse(service.firmware_update_in_progress)
            self.assertFalse(service.pending_firmware_path.exists())
            service._set_status.assert_any_call(
                "Connecting to GitHub for firmware 1.0.2"
            )
            service._set_status.assert_called_with(
                "Firmware update failed: network blocked"
            )
            service._notify_user.assert_called_with(
                "Firmware update failed: network blocked"
            )
            dialog.assert_called_once()
            service._update_device_menu.assert_called_once()


class ReconnectRecoveryTests(unittest.TestCase):
    def test_manual_sync_revives_stopped_worker(self) -> None:
        service = tray_app.TrayService.__new__(tray_app.TrayService)
        service.sync_now_event = threading.Event()
        service.test_mode_event = threading.Event()
        service.stop_event = threading.Event()
        service.stop_event.set()
        service.worker_lock = threading.Lock()
        service.worker = mock.Mock()
        service.worker.is_alive.return_value = False
        service.client_update_in_progress = False
        service._worker_main = mock.Mock()
        service._set_status = mock.Mock()
        replacement = mock.Mock()

        with mock.patch.object(
            tray_app.threading, "Thread", return_value=replacement
        ) as thread:
            service._sync_now(None, None)

        self.assertTrue(service.sync_now_event.is_set())
        self.assertFalse(service.stop_event.is_set())
        thread.assert_called_once_with(
            target=service._worker_main, daemon=True
        )
        replacement.start.assert_called_once_with()
        service._set_status.assert_called_once_with(
            "Sync worker restarted"
        )

    def test_old_pending_update_converges_to_rolled_back(self) -> None:
        service = tray_app.TrayService.__new__(tray_app.TrayService)
        service.update_lock = threading.Lock()
        service.device_info = {}
        service.firmware_update_in_progress = True
        service._queue_device_update_state = mock.Mock()
        service._set_status = mock.Mock()
        service._update_device_menu = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            service.pending_firmware_path = (
                Path(directory) / ".pending-firmware-update.json"
            )
            service.pending_firmware_path.write_text(
                json.dumps({
                    "version": "1.0.2",
                    "created_at": 1,
                }),
                encoding="utf-8",
            )

            service._device_info_updated({
                "version": "1.0.1",
                "ota": True,
            })

            self.assertFalse(service.pending_firmware_path.exists())

        self.assertFalse(service.firmware_update_in_progress)
        service._queue_device_update_state.assert_called_once_with(
            "ROLLED BACK"
        )
        service._set_status.assert_called_once_with(
            "Device rolled back to firmware 1.0.1"
        )
        service._update_device_menu.assert_called_once_with()

    def test_old_device_read_does_not_unlock_active_transfer(self) -> None:
        service = tray_app.TrayService.__new__(tray_app.TrayService)
        service.update_lock = threading.Lock()
        service.device_info = {}
        service.firmware_update_in_progress = True
        service.firmware_update_phase = tray_app.FIRMWARE_PHASE_TRANSFERRING
        service._queue_device_update_state = mock.Mock()
        service._set_status = mock.Mock()
        service._update_device_menu = mock.Mock()

        with tempfile.TemporaryDirectory() as directory:
            service.pending_firmware_path = (
                Path(directory) / ".pending-firmware-update.json"
            )
            service.pending_firmware_path.write_text(
                json.dumps({
                    "version": "1.0.3",
                    "created_at": 1,
                }),
                encoding="utf-8",
            )

            service._device_info_updated({
                "version": "1.0.2",
                "ota": True,
            })

            self.assertTrue(service.pending_firmware_path.exists())

        self.assertTrue(service.firmware_update_in_progress)
        self.assertEqual(
            tray_app.FIRMWARE_PHASE_TRANSFERRING,
            service.firmware_update_phase,
        )
        service._queue_device_update_state.assert_not_called()
        service._set_status.assert_not_called()
        service._update_device_menu.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
