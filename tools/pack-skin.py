"""Validate an editable skin folder and produce a .meterskin file."""
import argparse
import io
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
import skin_manager


def pack(source: Path, destination: Path):
    source = source.resolve(strict=True)
    if destination.exists():
        raise skin_manager.SkinError("Destination already exists; choose a new filename")
    files = sorted(path for path in source.rglob("*") if path.is_file())
    if not 7 <= len(files) <= 12 or sum(path.stat().st_size for path in files) > skin_manager.MAX_BYTES:
        raise skin_manager.SkinError("Source must contain only bounded skin files")
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            if not path.resolve().is_relative_to(source):
                raise skin_manager.SkinError("Source symlink leaves skin directory")
            archive.write(path, path.relative_to(source).as_posix())
    package = skin_manager.load_package(output.getvalue())
    with destination.open("xb") as stream:
        stream.write(package.archive)
    print(f"Validated: {package.name} {package.version}; {len(package.archive)} bytes; {package.sha256}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--compile", action="store_true", help="Compile an existing package to device binary")
    args = parser.parse_args()
    if args.compile:
        package = skin_manager.load_package(args.source)
        args.destination.write_bytes(package.wire)
        print(f"Compiled {package.id}: {len(package.wire)} bytes; {package.sha256}")
    else:
        pack(args.source, args.destination)
