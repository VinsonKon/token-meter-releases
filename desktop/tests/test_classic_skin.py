import asyncio
import io
import json
from pathlib import Path
import struct
import unittest
import zipfile
from unittest.mock import Mock, AsyncMock

import skin_manager as sm

ROOT = Path(__file__).resolve().parents[2]
CLASSIC = ROOT / "skins/codex-classic-glow-1.0.0.meterskin"
PIXEL = ROOT / "skins/codex-pixel-glow-1.0.0.meterskin"


class ClassicTests(unittest.TestCase):
    def setUp(self):
        with zipfile.ZipFile(CLASSIC) as z:
            self.files = {name:z.read(name) for name in z.namelist()}

    def edit(self, file, callback):
        value = json.loads(self.files[file])
        callback(value)
        self.files[file] = json.dumps(value).encode()
        with self.assertRaises(sm.SkinError):
            sm.compile_package(self.files)

    def test_pixel_identity_unchanged(self):
        pixel = sm.load_package(PIXEL)
        self.assertEqual(pixel.schema, 1)
        self.assertEqual(pixel.sha256, "3b4f0d1b04019ab822c6bc9e403fbe54f87a2de7d855cfc251d1b73c21ee8ad6")

    def test_classic_independent_and_offsets(self):
        classic = sm.load_package(CLASSIC)
        self.assertEqual(classic.schema,2)
        self.assertEqual(classic.wire[:4],b"TMS2")
        self.assertNotEqual(classic.id,sm.load_package(PIXEL).id)
        self.assertEqual(sm.PILL_BYTES,208)
        self.assertEqual(struct.unpack_from("<I",classic.wire,244+5*4)[0],900)
        self.assertEqual(sm.compile_package(self.files).wire,classic.wire)

    def test_pill_glow_cannot_reach_battery(self):
        self.edit("status.json",lambda v:v["states"]["waiting"].update(width=298,x=40+1))

    def test_pill_glow_cannot_reach_today_tokens(self):
        self.edit("status.json",lambda v:v["states"]["thinking"].update(y=36,height=44))

    def test_warning_cannot_be_truncated(self):
        self.edit("status.json",lambda v:v["states"]["waiting"].update(width=200))

    def test_dot_cannot_overlap_text(self):
        self.edit("status.json",lambda v:v["states"]["thinking"].update(dotX=36,dotSize=16))

    def test_forbid_arbitrary_renderer(self):
        self.edit("status.json",lambda v:v.update(renderer="script"))

    def test_do_not_accept_pill_as_schema_one(self):
        self.edit("manifest.json",lambda v:v.update(schema=1))

    def test_no_custom_warning_text(self):
        self.edit("status.json",lambda v:v["states"]["approval"].update(text="ALL GOOD"))

    def test_no_invisible_foreground_frame(self):
        self.edit("animations/approval.json",lambda v:v["frames"][0].__setitem__(1,0))

    def test_step_mode_is_explicit_and_pill_only(self):
        self.assertEqual(json.loads(self.files["animations/thinking.json"])["smooth"],2)
        with zipfile.ZipFile(PIXEL) as z:
            self.files = {name:z.read(name) for name in z.namelist()}
        self.edit("animations/thinking.json",lambda v:v.update(smooth=2))

    def test_static_old_sync_states(self):
        for state in ("stale","waiting"):
            frames=json.loads(self.files[f"animations/{state}.json"])["frames"]
            self.assertEqual(frames[0],frames[1])


class ClassicTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_device_rejected_before_any_write(self):
        client=Mock()
        client.read_gatt_char=AsyncMock(return_value=b'{"schema":1,"state":"active","id":"builtin"}')
        client.write_gatt_char=AsyncMock()
        with self.assertRaisesRegex(sm.SkinError,"1.1.2"):
            await sm.transfer(client,sm.SkinRequest(sm.load_package(CLASSIC),"device"))
        client.write_gatt_char.assert_not_called()

    async def test_schema_two_transfer(self):
        from test_skin_manager import FakeClient
        skin=sm.load_package(CLASSIC)
        client=FakeClient(skin)
        client.status["schemas"]=[1,2]
        result=await sm.transfer(client,sm.SkinRequest(skin,"device"))
        self.assertEqual(result["sha256"],skin.sha256)
        self.assertEqual(bytes(client.data),skin.wire)
