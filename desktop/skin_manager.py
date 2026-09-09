"""Declarative .meterskin packages; never extract archives or execute skin code."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import time
import tempfile
import zipfile

CONTROL_UUID = "7f520008-1b15-4f9f-8bd8-29dbe9e681a1"
DATA_UUID = "7f520009-1b15-4f9f-8bd8-29dbe9e681a1"
STATUS_UUID = "7f52000a-1b15-4f9f-8bd8-29dbe9e681a1"
MAX_BYTES = 384 * 1024
STATES = ("thinking", "approval", "stale", "waiting")
COLOR_NAMES = (
    "background header primaryText card track accent accentGlow muted secondaryText "
    "cardLabel cardHint status warning chargingText chargingFill lowBatteryText "
    "lowBatteryFill error limitStatus limitGlow exhaustedCard exhaustedTrack "
    "exhaustedLabel exhaustedHint activity"
).split()
# This version exposes dashboard geometry. System navigation/update hit areas
# stay firmware-owned; no executable expressions, URLs, or arbitrary bindings.
LAYOUT = {
    "statusBodyX": (34, 14, 34), "statusY": (24, 10, 36),
    "plateGap": (12, 0, 12), "bareGap": (10, 0, 12),
    "platePadding": (10, 0, 10), "statusRadius": (8, 0, 20),
    "totalX": (30, 20, 40), "totalBaseline": (181, 180, 205),
    "costX": (30, 20, 40), "costBaseline": (222, 218, 245),
    "labelX": (30, 20, 40), "labelBaseline": (115, 115, 125),
    "resetRight": (450, 430, 460), "cardX": (20, 10, 30),
    "cardWidth": (440, 400, 450), "primaryY": (251, 250, 375),
    "secondaryY": (361, 250, 375), "singleY": (261, 250, 280),
    "cardRadius": (20, 0, 30), "progressInset": (20, 15, 25),
    "progressY": (55, 50, 60), "progressHeight": (20, 10, 25),
    "progressRadius": (8, 0, 12), "singleResetY": (407, 395, 415),
    "singleFooterY": (447, 440, 455),
}
ANIMATION_WORDS = 28
HEADER_BYTES = 44 + 4 * (len(COLOR_NAMES) + len(LAYOUT)) + 4 * ANIMATION_WORDS * 4
PILL_FIELDS = ("x y width height radius textMode textInset baseline dotX dotY dotSize dots glowColor").split()
PILL_BYTES = 4 * len(PILL_FIELDS) * 4


def compile_pills(value):
    keys(value, ("renderer", "states"), "status")
    if value["renderer"] != "pill":
        raise SkinError("Unsupported status renderer")
    keys(value["states"], STATES, "pill states")
    limits = [(30,40),(10,36),(160,298),(36,44),(18,18),(0,2),(0,48),(24,30),(0,36),(0,28),(0,16),(0,1)]
    words = []
    for index, state in enumerate(STATES):
        p = value["states"][state]
        keys(p, PILL_FIELDS, state + " pill")
        values = [integer(p[name], low, high, name) for name,(low,high) in zip(PILL_FIELDS,limits)]
        text = (109,189,139,220)[index] + p["dots"]*18
        if (p["x"]+p["width"] > 338 or p["y"]+p["height"] > 69
                or (p["dots"] and (index != 0 or p["textMode"] != 2))
                or (p["textInset"]+text+8 > p["width"] if p["textMode"] == 2 else text+16 > p["width"])
                or p["dotSize"] and (p["textMode"] != 2 or p["dotX"]+p["dotSize"]+4 > p["textInset"]
                                       or p["dotY"]+p["dotSize"] > p["height"])):
            raise SkinError("Pill label, dot, or glow is outside its safe region")
        words += values + [color(p["glowColor"])]
    return struct.pack(f"<{len(words)}I", *words)


class SkinError(ValueError):
    pass


def integer(value, low, high, label):
    if type(value) is not int or not low <= value <= high:
        raise SkinError(f"{label}: expected integer {low}..{high}")
    return value


def color(value):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        raise SkinError("Colors must use #RRGGBB")
    return int(value[1:], 16)


def keys(value, expected, label):
    if not isinstance(value, dict) or set(value) != set(expected):
        raise SkinError(f"{label}: missing or unknown fields")


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SkinError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


@dataclass(frozen=True)
class Skin:
    name: str
    id: str
    version: str
    wire: bytes
    archive: bytes

    @property
    def schema(self):
        return struct.unpack_from("<I", self.wire, 4)[0]

    @property
    def sha256(self):
        return hashlib.sha256(self.wire).hexdigest()


def load_package(path: Path | bytes) -> Skin:
    if isinstance(path, Path):
        if path.stat().st_size > MAX_BYTES:
            raise SkinError("Skin archive is too large")
        raw = path.read_bytes()
    else:
        raw = path
    if len(raw) > MAX_BYTES:
        raise SkinError("Skin archive is too large")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            names = [entry.filename for entry in entries]
            allowed = {"manifest.json", "layout.json", "theme.json", "status.json"}
            allowed |= {f"animations/{state}.json" for state in STATES}
            allowed |= {f"animations/{state}.light" for state in STATES}
            if (len(names) != len(set(names)) or not set(names) <= allowed
                    or not 7 <= len(names) <= 12
                    or sum(entry.file_size for entry in entries) > MAX_BYTES):
                raise SkinError("Unexpected files, duplicate paths, or oversized skin")
            for entry in entries:
                if entry.flag_bits & 1 or entry.file_size > MAX_BYTES:
                    raise SkinError("Encrypted/oversized skin entry")
            files = {name: archive.read(name) for name in names}
        return compile_package(files, raw)
    except (KeyError, zipfile.BadZipFile, UnicodeError, json.JSONDecodeError,
            OverflowError, struct.error, TypeError, RecursionError) as exc:
        raise SkinError(f"Invalid skin package: {exc}") from exc


def compile_package(files, archive=b"") -> Skin:
    def read(name):
        data = files[name]
        if len(data) > 48 * 1024:
            raise SkinError("JSON entry is too large")
        return json.loads(data, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(SkinError("Nonfinite JSON")))
    meta = read("manifest.json")
    keys(meta, ("schema", "id", "name", "version", "screen"), "manifest")
    schema = integer(meta["schema"], 1, 2, "schema")
    if schema == 1 and "status.json" in files:
        raise SkinError("status.json requires skin schema 2")
    extra = compile_pills(read("status.json")) if schema == 2 else b""
    if not isinstance(meta["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", meta["id"]):
        raise SkinError("Skin id must be 1..31 lowercase ASCII letters/digits/hyphens")
    if not isinstance(meta["name"], str) or not 1 <= len(meta["name"]) <= 48 or any(ord(c) < 32 for c in meta["name"]):
        raise SkinError("Invalid skin display name")
    if not isinstance(meta["version"], str) or not re.fullmatch(r"\d{1,3}\.\d{1,3}\.\d{1,3}", meta["version"]):
        raise SkinError("Invalid skin version")
    if meta["screen"] != [480, 480]:
        raise SkinError("Only 480 x 480 skins are supported")
    theme = read("theme.json")
    keys(theme, COLOR_NAMES, "theme")
    colors = [color(theme[name]) for name in COLOR_NAMES]
    if max(colors[0] >> 16, (colors[0] >> 8) & 255, colors[0] & 255) > 48:
        raise SkinError("Skin v1 requires a dark background for readable system warnings")
    if min(colors[2] >> 16, (colors[2] >> 8) & 255, colors[2] & 255) < 192:
        raise SkinError("Primary text must remain light")
    layout = read("layout.json")
    keys(layout, LAYOUT, "layout")
    values = [integer(layout[name], low, high, name) for name, (_, low, high) in LAYOUT.items()]
    if (layout["cardX"] + layout["cardWidth"] > 470
            or abs(layout["primaryY"] - layout["secondaryY"]) < 100
            or layout["totalBaseline"] < layout["labelBaseline"] + 66
            or layout["costBaseline"] < layout["totalBaseline"] + 41
            or min(layout["primaryY"], layout["secondaryY"], layout["singleY"]) < layout["costBaseline"] + 15):
        raise SkinError("Dashboard widgets overlap or extend outside the screen")
    records, blob = [], bytearray()
    for state in STATES:
        animation = read(f"animations/{state}.json")
        keys(animation, ("kind", "smooth", "exact", "durations", "frames", "colors", "colorCycleMs",
                         "glow", "plate", "font", "textColor"), state)
        kind = {"light64": 0, "cells9": 1, "solid": 2, "pill": 3}.get(animation["kind"])
        if kind is None or (kind != 3 if schema == 2 else kind == 3):
            raise SkinError("Unknown animation kind")
        durations = animation["durations"]
        if not isinstance(durations, list) or not 2 <= len(durations) <= 128:
            raise SkinError("Animation requires 2..128 frames")
        ends, duration = [], 0
        for item in durations:
            duration += integer(item, 10, 5000, "frame duration")
            ends.append(duration)
        integer(duration, 200, 30000, "animation cycle")
        stride = (4096, 9, 1, 6)[kind]
        if kind == 0:
            if animation["frames"] != f"animations/{state}.light":
                raise SkinError("Invalid animation resource path")
            frames = files[animation["frames"]]
        else:
            frames = animation["frames"]
            if not isinstance(frames, list) or len(frames) != len(ends):
                raise SkinError("Frame count mismatch")
            if any(not isinstance(row, list) or len(row) != stride for row in frames):
                raise SkinError("Frame stride mismatch")
            frames = bytes(integer(v, 0, 255, "light") for row in frames for v in row)
        if len(frames) != len(ends) * stride or not any(frames):
            raise SkinError("Missing, blank, or incorrectly sized animation")
        if kind == 3:
            for at in range(0, len(frames), 6):
                if frames[at] > 15 or any(not 5 <= frames[at+i] <= 15 for i in (1,3,4,5)):
                    raise SkinError("Pill foreground and dots must remain visible (5..15)")
        palette = animation["colors"]
        if not isinstance(palette, list) or not 1 <= len(palette) <= 8:
            raise SkinError("Expected 1..8 animation colors")
        palette = [color(c) for c in palette]
        if kind == 3 and len(palette) != 1:
            raise SkinError("Pill requires one body color")
        if any(max(c >> 16, (c >> 8) & 255, c & 255) < 96 for c in palette):
            raise SkinError("Status animation is too dark")
        glow, plate = animation["glow"], animation["plate"]
        if not isinstance(glow, list) or len(glow) != 3 or not isinstance(plate, list) or len(plate) != 6:
            raise SkinError("Invalid glow/backplate track")
        glow = [integer(glow[0], 0, 255, "glow min"), integer(glow[1], glow[0], 255, "glow max"),
                integer(glow[2], 200, 30000, "glow cycle")]
        plate = [integer(plate[0], 0, 1, "plate"), integer(plate[1], 200, 30000, "plate cycle"),
                 integer(plate[2], 0, 255, "card min"), integer(plate[3], plate[2], 255, "card max"),
                 integer(plate[4], 0, 255, "halo min"), integer(plate[5], plate[4], 255, "halo max")]
        font = integer(animation["font"], 24 if kind == 3 else 28, 32, "font")
        if (font != 24 if kind == 3 else font not in (28, 32) or state == "waiting" and font != 28):
            raise SkinError("Supported status fonts are Bold 28/32; Waiting requires 28")
        ink = color(animation["textColor"])
        if max(ink >> 16, (ink >> 8) & 255, ink & 255) < 192:
            raise SkinError("Status text must remain readable")
        records += [kind, integer(animation["smooth"], 0, 2 if kind == 3 else 1, "smooth"),
                    integer(animation["exact"], 0, 1, "exact"), len(ends), duration,
                    HEADER_BYTES + len(extra) + len(blob), stride, len(palette),
                    integer(animation["colorCycleMs"], 200, 30000, "color cycle"),
                    *palette, *([0] * (8 - len(palette))), *glow, *plate, font, ink]
        blob += struct.pack(f"<{len(ends)}I", *ends) + frames
    size = HEADER_BYTES + len(extra) + len(blob)
    if size > MAX_BYTES:
        raise SkinError("Compiled skin exceeds 384 KiB")
    wire = struct.pack("<4sII32s", b"TMS1" if schema == 1 else b"TMS2", schema, size, meta["id"].encode())
    words = colors + values + records
    wire += struct.pack(f"<{len(words)}I", *words) + extra + blob
    assert len(wire) == size
    return Skin(meta["name"], meta["id"], meta["version"], wire, archive)


def import_package(path: Path, library: Path) -> Skin:
    skin = load_package(path)
    library.mkdir(parents=True, exist_ok=True)
    target = library / f"{skin.id}-{skin.sha256[:16]}.meterskin"
    # Content-addressed immutable library; cannot overwrite another skin/version.
    if target.exists():
        if load_package(target).sha256 != skin.sha256:
            raise SkinError("Stored skin does not match its library identity")
    else:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=library, prefix=".skin-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(skin.archive)
                stream.flush()
                os.fsync(stream.fileno())
            # The destination is derived only from validated id and content hash.
            # rename is non-overwriting on the supported Windows client.
            temporary.rename(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return skin


@dataclass(frozen=True)
class SkinRequest:
    skin: Skin | None
    address: str
    complete: object = None
    read_only: bool = False


def choose_file() -> Path | None:
    """Native Windows file chooser, without a Tcl/Tk runtime dependency."""
    import ctypes
    from ctypes import wintypes
    class OpenFileName(ctypes.Structure):
        _fields_ = [("lStructSize", wintypes.DWORD), ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE), ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR), ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD), ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD), ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD), ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR), ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD), ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR), ("lCustData", ctypes.c_ssize_t),
            ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", ctypes.c_void_p), ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD)]
    buffer = ctypes.create_unicode_buffer(32768)
    dialog = OpenFileName()
    dialog.lStructSize = ctypes.sizeof(dialog)
    dialog.lpstrFilter = "Token Meter 皮肤 (*.meterskin)\0*.meterskin\0\0"
    dialog.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
    dialog.nMaxFile = len(buffer)
    dialog.lpstrTitle = "导入 Token Meter 皮肤"
    dialog.Flags = 0x00001000 | 0x00000800 | 0x00080000 | 0x00000008
    library = ctypes.WinDLL("comdlg32", use_last_error=True)
    library.GetOpenFileNameW.argtypes = [ctypes.POINTER(OpenFileName)]
    library.GetOpenFileNameW.restype = wintypes.BOOL
    if library.GetOpenFileNameW(ctypes.byref(dialog)):
        return Path(buffer.value)
    error = library.CommDlgExtendedError()
    if error:
        raise SkinError(f"Windows file dialog failed: {error}")
    return None


async def read_status(client):
    if client.services.get_characteristic(STATUS_UUID) is None:
        raise SkinError("设备尚不支持换肤，请先安装支持 Skin v1 的固件")
    value = json.loads(bytes(await asyncio.wait_for(client.read_gatt_char(STATUS_UUID), 10)))
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise SkinError("Unsupported device skin protocol")
    return value


async def transfer(client, request: SkinRequest, progress=None, cancelled=None):
    """Same BLE connection as sync. Caller runs this concurrently with monitoring."""
    initial = await read_status(client)
    if initial.get("state") == "receiving":
        raise SkinError("设备正在接收其他皮肤，请稍后再试")
    if request.skin is not None:
        schemas = initial.get("schemas", [1])
        if (not isinstance(schemas, list) or any(type(s) is not int for s in schemas)
                or request.skin.schema not in schemas):
            raise SkinError("设备不支持此皮肤格式；原版呼吸皮肤需要 1.1.2 或更新固件")
        limit = initial.get("max_bytes", MAX_BYTES)
        if type(limit) is not int or limit <= 0:
            raise SkinError("Invalid device skin capacity")
        if len(request.skin.wire) > min(limit, MAX_BYTES):
            raise SkinError("皮肤超出设备容量，当前客户端上限为 384 KiB")
    operation = hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:12]
    async def command(op, **extra):
        await asyncio.wait_for(client.write_gatt_char(CONTROL_UUID, json.dumps({"op": op, "tx": operation, **extra},
                                    separators=(",", ":")).encode(), response=True), 10)
    async def wait_for(state, offset=0, timeout=30):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if cancelled and cancelled():
                raise SkinError("Skin operation cancelled")
            result = await read_status(client)
            if result.get("tx") == operation:
                if result.get("state") == "error":
                    raise SkinError(result.get("error", "Device rejected skin"))
                if result.get("state") == state and result.get("offset", 0) >= offset:
                    return result
            await asyncio.sleep(0.035)
        raise SkinError("Skin response timed out; current device skin retained unless commit completed")
    try:
        if request.skin is None:
            await command("default")
            result = await wait_for("active")
            if result.get("id") != "builtin":
                raise SkinError("Default skin readback mismatch")
            return result
        skin = request.skin
        await command("begin", size=len(skin.wire), sha256=skin.sha256)
        await wait_for("receiving")
        previous = -1
        for offset in range(0, len(skin.wire), 176):
            if cancelled and cancelled():
                raise SkinError("Skin transfer cancelled")
            chunk = skin.wire[offset:offset + 176]
            await asyncio.wait_for(client.write_gatt_char(DATA_UUID, struct.pack("<I", offset) + chunk, response=True), 10)
            await wait_for("receiving", offset + len(chunk), timeout=10)
            percent = (offset + len(chunk)) * 100 // len(skin.wire)
            if percent != previous and progress:
                progress(f"皮肤传输：{skin.name} {percent}%")
                previous = percent
            await asyncio.sleep(0)
        await command("commit")
        result = await wait_for("active")
        if result.get("sha256") != skin.sha256 or result.get("id") != skin.id:
            raise SkinError("Device active skin readback mismatch")
        return result
    except BaseException:
        try:
            await asyncio.wait_for(command("abort"), 2)
        except Exception:
            pass
        raise
