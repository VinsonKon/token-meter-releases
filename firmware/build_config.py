"""Select the ESP32-S3 Xtensa dynamic configuration for multi-target GCC."""

from pathlib import Path

Import("env")


platform = env.PioPlatform()
toolchain = platform.get_package_dir("toolchain-xtensa-esp-elf")
if toolchain:
    dynconfig = Path(toolchain) / "lib" / "xtensa_esp32s3.so"
    if dynconfig.exists():
        flag = f"-mdynconfig={dynconfig}"
        env.Append(CCFLAGS=[flag], LINKFLAGS=[flag])
        # A full dynconfig path is required by the Windows loader, while GCC's
        # multilib matcher only recognizes the basename. Select the matching
        # ESP32-S3 runtime directories explicitly so libc/libgcc use the same
        # little-endian target as application and framework objects.
        runtime = Path(toolchain) / "xtensa-esp-elf" / "lib" / "esp32s3"
        gcc_root = Path(toolchain) / "lib" / "gcc" / "xtensa-esp-elf"
        gcc_runtime = next(gcc_root.glob("*/esp32s3"), None)
        library_paths = [str(path) for path in (gcc_runtime, runtime)
                         if path and path.exists()]
        if library_paths:
            env.Prepend(LIBPATH=library_paths)
