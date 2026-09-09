import asyncio
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile
from unittest.mock import Mock

import skin_manager as sm

PACKAGE = Path(__file__).resolve().parents[2] / "skins" / "codex-pixel-glow-1.0.0.meterskin"


def archive(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return stream.getvalue()


class PackageTests(unittest.TestCase):
    def setUp(self):
        with zipfile.ZipFile(PACKAGE) as z:
            self.files = {name: z.read(name) for name in z.namelist()}

    def reject_edit(self, path, key, value):
        obj = json.loads(self.files[path])
        obj[key] = value
        self.files[path] = json.dumps(obj).encode()
        with self.assertRaises(sm.SkinError):
            sm.load_package(archive(self.files))

    def test_current_skin_is_independent_data(self):
        skin = sm.load_package(PACKAGE)
        self.assertEqual(skin.wire[:4], b"TMS1")
        self.assertEqual(struct.unpack_from("<I", skin.wire, 8)[0], len(skin.wire))
        self.assertEqual(sm.HEADER_BYTES, 692)
        self.assertEqual(skin.id, "codex-pixel-glow")

    def test_immutable_library_and_reimport(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first = sm.import_package(PACKAGE, root)
            second = sm.import_package(PACKAGE, root)
            self.assertEqual(first.sha256, second.sha256)
            self.assertEqual(len(list(root.iterdir())), 1)

    def test_traversal(self):
        self.files["../secret"] = b"x"
        with self.assertRaises(sm.SkinError): sm.load_package(archive(self.files))

    def test_scripts_rejected(self):
        self.files["script.py"] = b"print('bad')"
        with self.assertRaises(sm.SkinError): sm.load_package(archive(self.files))

    def test_duplicate_json(self):
        self.files["manifest.json"] = b'{"schema":1,"schema":1}'
        with self.assertRaises(sm.SkinError): sm.load_package(archive(self.files))

    def test_duplicate_zip_path(self):
        raw = io.BytesIO(archive(self.files))
        with zipfile.ZipFile(raw, "a") as z:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                z.writestr("manifest.json", self.files["manifest.json"])
        with self.assertRaises(sm.SkinError): sm.load_package(raw.getvalue())

    def test_compressed_bomb(self):
        self.files["animations/thinking.light"] = b"\0" * (sm.MAX_BYTES+1)
        with self.assertRaises(sm.SkinError): sm.load_package(archive(self.files))

    def test_new_capacity_and_oversized_compiled_animation(self):
        self.assertEqual(sm.MAX_BYTES, 384 * 1024)
        animation = json.loads(self.files["animations/thinking.json"])
        animation["durations"] = [10] * 100
        self.files["animations/thinking.json"] = json.dumps(animation).encode()
        self.files["animations/thinking.light"] = bytes([255]) * (100 * 4096)
        with self.assertRaisesRegex(sm.SkinError, "384 KiB"):
            sm.compile_package(self.files)

    def test_bad_resolution(self): self.reject_edit("manifest.json", "screen", [240,240])
    def test_bad_schema(self): self.reject_edit("manifest.json", "schema", True)
    def test_bad_id(self): self.reject_edit("manifest.json", "id", "../skin")
    def test_unknown_layout(self): self.reject_edit("layout.json", "execute", "command")
    def test_invalid_layout_type(self): self.reject_edit("layout.json", "statusY", 24.0)
    def test_card_overlap(self): self.reject_edit("layout.json", "secondaryY", 260)
    def test_header_overlap(self): self.reject_edit("layout.json", "labelBaseline", 125)
    def test_dark_text(self): self.reject_edit("theme.json", "primaryText", "#000000")
    def test_bright_background(self): self.reject_edit("theme.json", "background", "#FFFFFF")
    def test_zero_frame_duration(self): self.reject_edit("animations/stale.json", "durations", [0,0])
    def test_wrong_animation_path(self): self.reject_edit("animations/thinking.json", "frames", "../../oops")
    def test_blank_animation(self): self.reject_edit("animations/stale.json", "frames", [[0]]*60)
    def test_huge_font(self): self.reject_edit("animations/waiting.json", "font", 32)
    def test_unknown_animation(self): self.reject_edit("animations/stale.json", "kind", "script")
    def test_nonfinite(self): self.reject_edit("animations/stale.json", "colorCycleMs", float("nan"))
    def test_bad_color(self): self.reject_edit("animations/stale.json", "colors", ["#GGHHII"])
    def test_missing_file(self):
        del self.files["layout.json"]
        with self.assertRaises(sm.SkinError): sm.load_package(archive(self.files))


class FakeClient:
    def __init__(self, skin, fail_commit=False):
        self.services = Mock()
        self.services.get_characteristic.return_value = object()
        self.status = {"schema":1, "state":"active", "id":"builtin", "sha256":""}
        self.skin = skin
        self.data = bytearray()
        self.fail_commit = fail_commit

    async def read_gatt_char(self, uuid):
        return json.dumps(self.status).encode()

    async def write_gatt_char(self, uuid, value, response):
        await asyncio.sleep(0)
        if uuid == sm.DATA_UUID:
            offset = struct.unpack_from("<I", value)[0]
            assert offset == len(self.data)
            self.data.extend(value[4:])
            self.status["offset"] = len(self.data)
            return
        command = json.loads(value)
        self.status["tx"] = command["tx"]
        if command["op"] == "begin":
            self.status.update(state="receiving", offset=0)
        elif command["op"] == "commit":
            if self.fail_commit:
                self.status.update(state="error", error="hash mismatch")
            else:
                self.status.update(state="active", id=self.skin.id, sha256=self.skin.sha256)
        elif command["op"] == "default":
            self.status.update(state="active", id="builtin", sha256="", offset=0)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self): self.skin = sm.load_package(PACKAGE)

    async def test_full_transfer_and_verified_readback(self):
        client = FakeClient(self.skin)
        result = await sm.transfer(client, sm.SkinRequest(self.skin, "address"))
        self.assertEqual(bytes(client.data), self.skin.wire)
        self.assertEqual(result["sha256"], self.skin.sha256)

    async def test_default(self):
        result = await sm.transfer(FakeClient(self.skin), sm.SkinRequest(None, "address"))
        self.assertEqual(result["id"], "builtin")

    async def test_capacity_rejected_before_any_control_write(self):
        client = FakeClient(self.skin)
        client.status["max_bytes"] = len(self.skin.wire) - 1
        with self.assertRaisesRegex(sm.SkinError, "容量"):
            await sm.transfer(client, sm.SkinRequest(self.skin, "address"))
        self.assertNotIn("tx", client.status)
        self.assertEqual(bytes(client.data), b"")

    async def test_bad_device_capacity_rejected(self):
        for value in (True, 0, -1, "393216"):
            client = FakeClient(self.skin)
            client.status["max_bytes"] = value
            with self.assertRaisesRegex(sm.SkinError, "capacity"):
                await sm.transfer(client, sm.SkinRequest(self.skin, "address"))
            self.assertNotIn("tx", client.status)

    async def test_default_ignores_capacity(self):
        client = FakeClient(self.skin)
        client.status["max_bytes"] = 0
        result = await sm.transfer(client, sm.SkinRequest(None, "address"))
        self.assertEqual(result["id"], "builtin")

    async def test_old_firmware_is_read_only(self):
        client = FakeClient(self.skin)
        client.services.get_characteristic.return_value = None
        with self.assertRaises(sm.SkinError): await sm.transfer(client, sm.SkinRequest(self.skin, "x"))
        self.assertEqual(len(client.data), 0)

    async def test_rejected_commit_is_not_success(self):
        client = FakeClient(self.skin, True)
        with self.assertRaisesRegex(sm.SkinError, "hash mismatch"):
            await sm.transfer(client, sm.SkinRequest(self.skin, "x"))
        self.assertEqual(client.status["id"], "builtin")

    async def test_cancel(self):
        client = FakeClient(self.skin)
        with self.assertRaises(sm.SkinError):
            await sm.transfer(client, sm.SkinRequest(self.skin, "x"), cancelled=lambda: True)
        self.assertEqual(len(client.data), 0)

    async def test_readback_mismatch(self):
        client = FakeClient(self.skin)
        original = client.write_gatt_char
        async def tamper(uuid, value, response):
            await original(uuid, value, response)
            if client.status["state"] == "active": client.status["sha256"] = "0"*64
        client.write_gatt_char = tamper
        with self.assertRaisesRegex(sm.SkinError, "readback"):
            await sm.transfer(client, sm.SkinRequest(self.skin, "x"))


if __name__ == "__main__": unittest.main()
