import asyncio
from pathlib import Path
import queue
import threading
import tempfile
import unittest
from unittest import mock

import skin_manager
import sync
import tray_app
import update_manager

PACKAGE = Path(__file__).resolve().parents[2] / "skins" / "codex-pixel-glow-1.0.0.meterskin"


def service():
    app = tray_app.TrayService.__new__(tray_app.TrayService)
    app.skin_lock = threading.Lock()
    app.update_lock = threading.Lock()
    app.skin_in_progress = False
    app.firmware_update_in_progress = app.client_update_in_progress = False
    app.device_info = {"skin": 1}
    app.bound_address = "device-a"
    app.skins = {}
    app.skin_status = "unknown"
    app.active_skin_sha = ""
    app.skin_queue = queue.Queue(maxsize=1)
    app.sync_now_event = threading.Event()
    app._set_status = mock.Mock()
    app._notify_user = mock.Mock()
    app._update_device_menu = mock.Mock()
    return app


class TraySkinTests(unittest.TestCase):
    def test_two_independent_packages_reload_and_select_from_menu(self):
        classic = PACKAGE.with_name("codex-classic-glow-1.0.0.meterskin")
        app = service()
        with tempfile.TemporaryDirectory() as folder:
            library = Path(folder)
            for path in (PACKAGE, classic):
                skin_manager.import_package(path, library)
            # Same content-addressed packages loaded on client startup.
            for path in library.glob("*.meterskin"):
                skin = skin_manager.load_package(path)
                app.skins[skin.sha256] = skin
            self.assertEqual(len(app.skins), 2)
            self.assertEqual({s.schema for s in app.skins.values()}, {1, 2})
            for skin in app.skins.values():
                items = list(app._skin_menu_items())
                self.assertEqual(len(items), 6)
                action = next(item for item in items[4:] if skin.name in item.text)
                action(None)
                request = app.skin_queue.get_nowait()
                self.assertEqual(request.skin.sha256, skin.sha256)
                request.complete({"id": skin.id, "sha256": skin.sha256}, None)
                marked = [item.text for item in list(app._skin_menu_items())[4:] if item.checked]
                self.assertEqual(marked, [f"{skin.name} {skin.version}"])
            list(app._skin_menu_items())[3](None)
            request = app.skin_queue.get_nowait()
            self.assertIsNone(request.skin)
            request.complete({"id": "builtin", "sha256": ""}, None)
            self.assertFalse(any(item.checked for item in list(app._skin_menu_items())[4:]))

    def test_menu_actions_have_supported_signature(self):
        app = service()
        skin = skin_manager.load_package(PACKAGE)
        app.skins[skin.sha256] = skin
        items = list(app._skin_menu_items())
        self.assertEqual(len(items), 5)
        self.assertIn(skin.name, items[-1].text)

    def test_active_marker_only_after_device_readback(self):
        app = service()
        skin = skin_manager.load_package(PACKAGE)
        app.skins[skin.sha256] = skin
        app._queue_skin(skin)
        self.assertEqual(app.active_skin_sha, "")
        request = app.skin_queue.get_nowait()
        self.assertEqual(request.address, "device-a")
        request.complete({"id": skin.id, "sha256": skin.sha256}, None)
        self.assertEqual(app.active_skin_sha, skin.sha256)
        self.assertFalse(app.skin_in_progress)

    def test_unknown_firmware_does_not_queue_writes(self):
        app = service(); app.device_info = {}
        with mock.patch.object(tray_app, "error_dialog"):
            app._queue_skin(None)
        self.assertTrue(app.skin_queue.empty())

    def test_ota_exclusion(self):
        app = service(); app.firmware_update_in_progress = True
        with mock.patch.object(tray_app, "error_dialog"):
            app._queue_skin(None)
        self.assertTrue(app.skin_queue.empty())

    def test_read_only_request(self):
        app = service(); app._queue_skin(None, read_only=True)
        self.assertTrue(app.skin_queue.get_nowait().read_only)

    def test_binding_change_clears_old_marker(self):
        app = service(); app.active_skin_sha = "old"
        app._reset_skin_device()
        self.assertEqual(app.active_skin_sha, "")
        self.assertNotIn("skin", app.device_info)

    def test_import_failure_does_not_claim_activation(self):
        app = service(); app._queue_skin(None)
        app.skin_queue.get_nowait().complete(None, "disconnected")
        self.assertFalse(app.skin_in_progress)
        self.assertEqual(app.active_skin_sha, "")


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_info_preserves_skin_capability(self):
        client = mock.Mock()
        client.read_gatt_char = mock.AsyncMock(return_value=b'{"version":"1.1.0","skin":1,"ota":true}')
        info = await update_manager.read_device_info(client)
        self.assertEqual(info["skin"], 1)

    async def test_sync_continues_while_skin_transfer_waits(self):
        started, release = asyncio.Event(), asyncio.Event()
        writes = []
        stop, forced, test_mode = threading.Event(), threading.Event(), threading.Event()
        test_mode.set()
        async def transfer(*args):
            started.set()
            await release.wait()
            return {"id":"builtin"}
        async def write(*args, **kwargs): writes.append(args[1])
        async def watcher(*args, **kwargs): await asyncio.Event().wait()
        requests = queue.Queue()
        requests.put(skin_manager.SkinRequest(None, "device-a"))
        client = mock.Mock(is_connected=True)
        client.start_notify = mock.AsyncMock()
        args = sync.parse_args([])
        with tempfile.TemporaryDirectory() as folder, \
                mock.patch.object(skin_manager, "transfer", side_effect=transfer), \
                mock.patch.object(sync, "watch_usage_files", side_effect=watcher), \
                mock.patch.object(sync, "write_packet_with_ack", side_effect=write):
            root = Path(folder)
            task = asyncio.create_task(sync.monitor_connection(client, "device-a", args, root, root,
                root/"cache", 7.0, "test", stop, forced, test_mode, threading.Event(), None,
                skin_queue=requests))
            try:
                await asyncio.wait_for(started.wait(), 2)
                forced.set()
                deadline = asyncio.get_running_loop().time() + 2.5
                while len(writes) < 2 and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.02)
                self.assertGreaterEqual(len(writes), 2)
                self.assertFalse(release.is_set())
            finally:
                release.set(); stop.set()
                await asyncio.wait_for(task, 3)


if __name__ == "__main__": unittest.main()
