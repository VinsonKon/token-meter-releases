#!/usr/bin/env python3
"""Development-only Codex Meter tray host with display test controls."""

from tray_app import main


if __name__ == "__main__":
    raise SystemExit(main(test_build=True))
