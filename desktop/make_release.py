#!/usr/bin/env python3
"""Prepare fixed-name GitHub Release assets and update.json."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil

import release_config
import skin_manager


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(128 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    output = (args.output or project / "github-release").resolve()
    version = (project / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"VERSION is not semantic x.y.z: {version}")
    if version != release_config.APP_VERSION:
        raise SystemExit(
            f"VERSION ({version}) and APP_VERSION "
            f"({release_config.APP_VERSION}) disagree"
        )
    firmware_header = (
        project / "firmware" / "include" / "firmware_version.h"
    ).read_text(encoding="utf-8")
    match = re.search(r'kFirmwareVersion\[\]\s*=\s*"([^"]+)"', firmware_header)
    firmware_version = match.group(1) if match else ""
    if not re.fullmatch(r"\d+\.\d+\.\d+", firmware_version):
        raise SystemExit(
            f"Firmware version is invalid: {firmware_version or 'missing'}"
        )
    # Client-only releases can carry the existing verified firmware. Its own
    # version remains the protocol compatibility floor; never relabel bytes as
    # a firmware update just because the desktop application changed.
    if tuple(map(int, firmware_version.split("."))) > tuple(map(int, version.split("."))):
        raise SystemExit("Client version is older than the firmware compatibility floor")

    names = (
        release_config.CLIENT_ASSET_NAME,
        release_config.FIRMWARE_OTA_ASSET_NAME,
        release_config.FIRMWARE_FACTORY_ASSET_NAME,
    )
    sources = {name: project / name for name in names}
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise SystemExit("Missing release assets: " + ", ".join(missing))

    skin_sources = [project / "skins" / name for name in (
        "codex-classic-glow-1.0.0.meterskin",
        "codex-pixel-glow-1.0.0.meterskin",
    )]
    skins = [skin_manager.load_package(path) for path in skin_sources]
    if len({skin.id for skin in skins}) != len(skins):
        raise SystemExit("Release skins must have independent identities")

    output.mkdir(parents=True, exist_ok=True)
    (output / "CodexMeterUpdater.exe").unlink(missing_ok=True)
    for name, source in sources.items():
        shutil.copy2(source, output / name)
    for source in skin_sources:
        shutil.copy2(source, output / source.name)

    manifest = {
        "schema": 1,
        "channel": "stable",
        "repository": release_config.RELEASE_REPOSITORY,
        "tag": f"v{version}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "client": {
            "version": version,
            "asset": release_config.CLIENT_ASSET_NAME,
            "size": sources[
                release_config.CLIENT_ASSET_NAME
            ].stat().st_size,
            "sha256": sha256(sources[release_config.CLIENT_ASSET_NAME]),
        },
        "firmware": {
            "version": firmware_version,
            "board": "waveshare-amoled-2.16",
            "min_client": firmware_version,
            "min_battery": 30,
            "asset": release_config.FIRMWARE_OTA_ASSET_NAME,
            "size": sources[
                release_config.FIRMWARE_OTA_ASSET_NAME
            ].stat().st_size,
            "sha256": sha256(
                sources[release_config.FIRMWARE_OTA_ASSET_NAME]
            ),
        },
    }
    manifest_text = json.dumps(manifest, indent=2) + "\n"
    (output / release_config.MANIFEST_ASSET_NAME).write_text(manifest_text, encoding="utf-8")
    # Keep the source checkout and full distribution consistent with the assets.
    (project / release_config.MANIFEST_ASSET_NAME).write_text(manifest_text, encoding="utf-8")
    print(f"Prepared GitHub Release assets in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
