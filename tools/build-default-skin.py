"""Export the approved UI into editable JSON/resources and a .meterskin archive."""
from pathlib import Path
import argparse
import json
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
import skin_manager as skin


def numbers(header, name):
    text = (ROOT / "firmware" / "include" / header).read_text(encoding="utf-8")
    body = re.search(r"\b" + re.escape(name) + r"\[.*?=\s*\{(.*?)\};", text, re.S).group(1)
    return [int(x, 0) for x in re.findall(r"0x[0-9a-fA-F]+|\b\d+\b", body)]


def build(destination):
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / "codex-pixel-glow"
    (source / "animations").mkdir(parents=True, exist_ok=True)
    files = {}
    def add(path, value):
        files[path] = json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    add("manifest.json", {"schema": 1, "id": "codex-pixel-glow", "name": "Codex Pixel Glow",
                          "version": "1.0.0", "screen": [480, 480]})
    add("layout.json", {key: values[0] for key, values in skin.LAYOUT.items()})
    theme_text = (ROOT / "firmware" / "src" / "ui_theme.cpp").read_text(encoding="utf-8")
    theme = {}
    for name in skin.COLOR_NAMES:
        match = re.search(r"rgb565\((\d+),\s*(\d+),\s*(\d+)\),\s*// " + name + r"\s", theme_text)
        theme[name] = "#" + "".join(f"{int(value):02X}" for value in match.groups())
    add("theme.json", theme)
    ends = numbers("thinking_reference.h", "kFrameEnds")
    durations = [value - (ends[i-1] if i else 0) for i, value in enumerate(ends)]
    files["animations/thinking.light"] = bytes(numbers("thinking_reference.h", "kLightFrames"))
    def animation(kind, durations, frames, colors, cycle, glow, plate, font=32, ink="#FFFFFF", smooth=0, exact=1):
        return {"kind": kind, "smooth": smooth, "exact": exact, "durations": durations,
                "frames": frames, "colors": colors, "colorCycleMs": cycle, "glow": glow,
                "plate": plate, "font": font, "textColor": ink}
    add("animations/thinking.json", animation("light64", durations, "animations/thinking.light",
        ["#FFD280", "#78E89E", "#6AB8FF"], 12000, [140,140,1500], [1,1500,45,105,60,165]))
    approval = numbers("approval_reference.h", "kSamples")
    frames = []
    for at in range(0, len(approval), 3):
        groups = approval[at:at+3]
        frames.append([groups[2 if i == 4 else 0 if i % 2 == 0 else 1] for i in range(9)])
    add("animations/approval.json", animation("cells9", [20]*len(frames), frames,
        ["#FF6B6B"], 1400, [112,168,3200], [1,1400,65,130,85,210]))
    stale = numbers("reading_reference.h", "kLightSamples")
    add("animations/stale.json", animation("solid", [20]*len(stale), [[v] for v in stale],
        ["#E6AC38"], 1200, [163,163,1200], [0,1200,0,0,0,0], ink="#FFD280"))
    ring = [0,1,2,5,8,7,6,3]
    frames = []
    for cell in ring:
        frame = [77]*9
        frame[4] = 0
        frame[cell] = 242
        frames.append(frame)
    add("animations/waiting.json", animation("cells9", [400]*8, frames,
        ["#90BBE3"], 3200, [102,153,3200], [0,3200,0,0,0,0], font=28, smooth=1, exact=0))
    for path, data in files.items():
        (source / path).write_bytes(data)
    target = destination / "codex-pixel-glow-1.0.0.meterskin"
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    package = skin.load_package(target)
    (destination / "codex-pixel-glow.skinbin").write_bytes(package.wire)
    print(f"{target}\narchive={target.stat().st_size} wire={len(package.wire)} sha256={package.sha256}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "skins")
    build(parser.parse_args().output)
