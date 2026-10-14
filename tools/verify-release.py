"""Validate the complete release bundle without reading local account state."""
import argparse
import hashlib
from itertools import chain
import json
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
import release_config
import skin_manager

SKINS = ("codex-classic-glow-1.0.0.meterskin", "codex-pixel-glow-1.0.0.meterskin")
ASSETS = ("CodexMeterSync.exe", "codex-token-monitor.ota.bin", "codex-token-monitor.factory.bin",
          "update.json", *SKINS, "codex-token-monitor.zip")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def validate(folder):
    version = (ROOT / "VERSION").read_text().strip()
    assert version == release_config.APP_VERSION
    header = (ROOT / "firmware/include/firmware_version.h").read_text()
    firmware_match = re.search(r'kFirmwareVersion\[\]\s*=\s*"(\d+\.\d+\.\d+)"', header)
    assert firmware_match, "Firmware version is missing"
    firmware_version = firmware_match[1]
    assert tuple(map(int, firmware_version.split("."))) <= tuple(map(int, version.split(".")))
    files = {name: (folder / name).read_bytes() for name in ASSETS}
    manifest = json.loads(files["update.json"])
    assert manifest["schema"] == 1 and manifest["channel"] == "stable"
    assert manifest["tag"] == "v" + version and manifest["repository"] == release_config.RELEASE_REPOSITORY
    for section in ("client", "firmware"):
        item = manifest[section]
        assert item["version"] == (version if section == "client" else firmware_version)
        assert len(files[item["asset"]]) == item["size"] and digest(files[item["asset"]]) == item["sha256"]
    assert manifest["firmware"]["min_client"] == firmware_version
    assert manifest["firmware"]["board"] == "waveshare-amoled-2.16"
    assert (firmware_version + "\0").encode() in files["codex-token-monitor.ota.bin"]
    ota = files["codex-token-monitor.ota.bin"]
    assert files["codex-token-monitor.factory.bin"][0x10000:0x10000+len(ota)] == ota
    for name in ASSETS[:4]:
        assert files[name] == (ROOT / name).read_bytes(), "Root asset differs: " + name
    for name in SKINS:
        skin = skin_manager.load_package(files[name])
        assert skin.archive == (ROOT / "skins" / name).read_bytes()
        print(f"Skin: {skin.id}; schema={skin.schema}; wire={len(skin.wire)}; sha256={skin.sha256}")
    forbidden = {"backup", "backups", "__pycache__", ".pio", ".git", ".pytest_cache", "updates"}
    with zipfile.ZipFile(folder / "codex-token-monitor.zip") as z:
        names = z.namelist()
        assert len(names) == len(set(names)), "Duplicate archive paths"
        for entry in z.infolist():
            path = PurePosixPath(entry.filename)
            assert not path.is_absolute() and ".." not in path.parts and "\\" not in entry.filename
            assert not set(path.parts) & forbidden
            assert not any(p.startswith(".venv") for p in path.parts)
            assert not any(marker in path.name.lower() for marker in ("full-flash", "device-address", "codex-account", "credential"))
            assert path.suffix not in (".log", ".pyc", ".pyo", ".skinbin", ".env")
        for name in ASSETS[:4]:
            assert z.read(name) == files[name], "ZIP asset differs: " + name
        for name in SKINS:
            assert z.read("skins/"+name) == files[name]
        assert z.read("VERSION").decode().strip() == version
        for source_root in ("desktop", "firmware/include", "firmware/src", "firmware/assets", "firmware/tests", "skins", "tools"):
            paths = (chain((ROOT / "desktop").glob("*"),
                           (ROOT / "desktop/assets").rglob("*"),
                           (ROOT / "desktop/tests").rglob("*"))
                     if source_root == "desktop" else (ROOT / source_root).rglob("*"))
            for path in paths:
                relative = path.relative_to(ROOT).as_posix()
                # Desktop builds/environments are not source trees.
                if source_root == "desktop" and len(path.relative_to(ROOT / "desktop").parts) > 1:
                    if path.relative_to(ROOT / "desktop").parts[0] not in ("assets", "tests"):
                        continue
                if not path.is_file() or set(path.parts) & {"__pycache__", ".pytest_cache"} or path.suffix in (".pyc", ".pyo", ".skinbin"):
                    continue
                if source_root == "desktop" and path.parent == ROOT / "desktop" and path.suffix not in (".py", ".cs", ".cmd", ".ps1", ".txt"):
                    continue
                assert z.read(relative) == path.read_bytes(), "ZIP source differs: " + relative
        print(f"Full ZIP: {len(names)} entries; source/binary/skin equality and private-file exclusions passed")
    return {name: digest(files[name]) for name in ASSETS}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=ROOT / "github-release")
    parser.add_argument("--write-checksums", action="store_true")
    args = parser.parse_args()
    hashes = validate(args.directory)
    checksums = "".join(f"{value}  {name}\n" for name, value in hashes.items())
    target = args.directory / "SHA256SUMS.txt"
    if args.write_checksums:
        target.write_text(checksums, encoding="ascii")
    else:
        assert target.read_text(encoding="ascii") == checksums, "Release checksum list differs"
    print("PASS: versions, seven release assets, full ZIP and SHA-256 inventory")
