"""Export the pre-pixel v1.0.11 style as independent schema-2 skin data.

Only creates new output paths. Does not flash, import, select, or publish skins.
The opacity timeline is sampled from the old drawStatus formula at 20 ms;
geometry/fonts/glow masks match c692aca, while current state priority is kept.
"""
import argparse
import io
import json
import math
from pathlib import Path
import re
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
import skin_manager as sm


def contents():
    source = subprocess.check_output(
        ["git", "show", "c692aca:firmware/src/ui_theme.cpp"], cwd=ROOT).decode("utf-8")
    theme = {}
    for name in sm.COLOR_NAMES:
        match = re.search(r"rgb565\((\d+),\s*(\d+),\s*(\d+)\),\s*// " + name + r"\s", source)
        if not match:
            raise RuntimeError("Original theme field missing: " + name)
        theme[name] = "#" + "".join(f"{int(v):02X}" for v in match.groups())
    files = {}
    def add(name, value):
        files[name] = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    add("manifest.json", {"schema":2, "id":"codex-classic-glow", "name":"原版呼吸 · Classic Glow",
                          "version":"1.0.0", "screen":[480,480]})
    layout = {name: values[0] for name,values in sm.LAYOUT.items()}
    layout.update(statusY=28, statusRadius=18)
    add("layout.json", layout)
    add("theme.json", theme)
    geometries = {}
    for index,state in enumerate(sm.STATES):
        geometry = dict(zip(sm.PILL_FIELDS, [30,28,(190,230,180,260)[index],36,18,
            (2,1,0,0)[index],40 if index == 0 else 0,27,20,12,12 if index == 0 else 0,
            1 if index == 0 else 0,theme[("status","limitGlow","warning","warning")[index]]]))
        geometries[state] = geometry
        frames = []
        if index < 2:
            for elapsed in range(0,1400,20):
                phase = elapsed/1400
                normalized = .5-.5*math.cos(math.tau*phase)
                normalized = normalized*normalized*(3-2*normalized)
                pulse = .30+.70*normalized
                opacity = int(pulse*15+.5)
                glow = int(64+(230-64)*normalized**1.4+.5)
                dots = [int(min(1,max(.30,pulse*(.45+.55*(.5+.5*math.sin(math.tau*(phase*2-i/3))))))*15+.5)
                        for i in range(3)]
                frames.append([opacity,opacity,glow,*dots])
            durations = [20]*len(frames)
        else:
            frames = [[15,15,255,15,15,15]]*2
            durations = [700,700]
        add(f"animations/{state}.json", {"kind":"pill", "smooth":2, "exact":1,
            "durations":durations, "frames":frames,
            "colors":[theme[("status","limitStatus","warning","warning")[index]]],
            "colorCycleMs":1400, "glow":[0,0,1400], "plate":[1,1400,255,255,255,255],
            "font":24, "textColor":theme["primaryText"]})
    add("status.json", {"renderer":"pill", "states":geometries})
    return files


def build(destination):
    source = destination / "codex-classic-glow"
    target = destination / "codex-classic-glow-1.0.0.meterskin"
    if source.exists() or target.exists():
        raise RuntimeError("Output already exists; refusing overwrite")
    files = contents()
    data = io.BytesIO()
    with zipfile.ZipFile(data,"w",zipfile.ZIP_DEFLATED) as z:
        for name,value in files.items():
            info = zipfile.ZipInfo(name, (2026,9,9,0,0,0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info,value)
    skin = sm.load_package(data.getvalue())
    (source / "animations").mkdir(parents=True)
    for name,value in files.items():
        (source / name).write_bytes(value)
    with target.open("xb") as stream:
        stream.write(skin.archive)
    print(f"{target}: schema={skin.schema}, archive={len(skin.archive)}, wire={len(skin.wire)}, sha256={skin.sha256}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output",type=Path,default=ROOT / "skins")
    build(parser.parse_args().output)
