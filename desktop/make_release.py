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
    if firmware_version != version:
        raise SystemExit(
            f"VERSION ({version}) and firmware version "
            f"({firmware_version or 'missing'}) disagree"
        )

    names = (
        release_config.CLIENT_ASSET_NAME,
        release_config.FIRMWARE_OTA_ASSET_NAME,
        release_config.FIRMWARE_FACTORY_ASSET_NAME,
    )
    sources = {name: project / name for name in names}
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise SystemExit("Missing release assets: " + ", ".join(missing))

    output.mkdir(parents=True, exist_ok=True)
    (output / "CodexMeterUpdater.exe").unlink(missing_ok=True)
    for name, source in sources.items():
        shutil.copy2(source, output / name)

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
            "version": version,
            "board": "waveshare-amoled-2.16",
            "min_client": version,
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
    (output / release_config.MANIFEST_ASSET_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Prepared GitHub Release assets in {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
