#!/usr/bin/env python3
"""Generate C# assembly metadata for the hidden Windows launcher."""

from __future__ import annotations

from pathlib import Path
import re
import sys


def main() -> int:
    desktop = Path(__file__).resolve().parent
    project = desktop.parent
    version = (project / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"VERSION is not semantic x.y.z: {version}")

    output = desktop / "build-launcher" / "AssemblyInfo.cs"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "\n".join(
            (
                "using System.Reflection;",
                "",
                '[assembly: AssemblyTitle("Codex Meter Sync")]',
                '[assembly: AssemblyDescription("Codex Meter BLE tray synchronizer")]',
                '[assembly: AssemblyCompany("VinsonKon")]',
                '[assembly: AssemblyProduct("Codex Meter Sync")]',
                f'[assembly: AssemblyVersion("{version}.0")]',
                f'[assembly: AssemblyFileVersion("{version}.0")]',
                f'[assembly: AssemblyInformationalVersion("{version}")]',
                "",
            )
        ),
        encoding="utf-8",
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
