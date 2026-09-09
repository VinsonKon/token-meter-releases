#!/usr/bin/env python3
"""Windows system-tray host for the Codex Meter BLE sync service."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import sys
import threading
import time
import traceback
import winreg

# PyInstaller's Windows GUI bootloader intentionally provides no console and
# therefore initializes these streams as None. Some transitive tray/BLE imports
# probe or write them during module import, before our own safe logger exists.
# Give those imports a harmless sink without allocating a terminal window.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")


def enable_high_dpi_awareness() -> None:
    """Keep native tray menus and dialogs sharp on scaled Windows displays."""
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = (wintypes.HANDLE,)
        set_context.restype = wintypes.BOOL
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        if set_context(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = (ctypes.c_int,)
        set_awareness.restype = ctypes.c_long
        # PROCESS_PER_MONITOR_DPI_AWARE
        if set_awareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


# DPI awareness must be selected before importing or creating any UI backend.
enable_high_dpi_awareness()

from PIL import Image
import pystray

import source_registry
import sync as meter_sync
import release_config
import update_manager
import skin_manager


APP_NAME = "CodexMeterSync"
ICON_ASSET_NAME = "codex-meter-icon.png"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MUTEX_NAME = "Local\\CodexMeterSync.Tray"
SYNC_LOG_PREFIX = "sync-"
SYNC_LOG_SUFFIX = ".log"
FIRMWARE_PHASE_IDLE = "idle"
FIRMWARE_PHASE_CONFIRMING = "confirming"
FIRMWARE_PHASE_DOWNLOADING = "downloading"
FIRMWARE_PHASE_QUEUED = "queued"
FIRMWARE_PHASE_TRANSFERRING = "transferring"
FIRMWARE_PHASE_AWAITING_VERIFICATION = "awaiting_verification"
FIRMWARE_TRANSFER_ACTIVE_PHASES = frozenset({
    FIRMWARE_PHASE_CONFIRMING,
    FIRMWARE_PHASE_DOWNLOADING,
    FIRMWARE_PHASE_QUEUED,
    FIRMWARE_PHASE_TRANSFERRING,
})
_mutex_handle = None


def sync_log_path(state_dir: Path, now: datetime | None = None) -> Path:
    current = now or datetime.now().astimezone()
    return state_dir / f"{SYNC_LOG_PREFIX}{current:%Y-%m-%d}{SYNC_LOG_SUFFIX}"


def prune_sync_logs(state_dir: Path, now: datetime | None = None) -> None:
    current = now or datetime.now().astimezone()
    keep = {
        sync_log_path(state_dir, current).name,
        sync_log_path(state_dir, current - timedelta(days=1)).name,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    for path in state_dir.glob("*.log"):
        if path.name not in keep:
            path.unlink(missing_ok=True)


def append_sync_log(
    state_dir: Path, message: str, now: datetime | None = None
) -> Path:
    current = now or datetime.now().astimezone()
    prune_sync_logs(state_dir, current)
    path = sync_log_path(state_dir, current)
    with path.open("a", encoding="utf-8") as log:
        log.write(message.rstrip("\n") + "\n")
    return path


def write_boot_diagnostic(message: str) -> None:
    if not getattr(sys, "frozen", False):
        return
    try:
        now = datetime.now().astimezone()
        append_sync_log(
            meter_sync.state_directory(),
            f"[{now:%Y-%m-%d %H:%M:%S}] {message}",
            now,
        )
    except OSError:
        pass


def hide_console_window() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import ctypes

    window = ctypes.windll.kernel32.GetConsoleWindow()
    if window:
        ctypes.windll.user32.ShowWindow(window, 0)


def acquire_single_instance() -> bool:
    global _mutex_handle
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    ctypes.set_last_error(0)
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return bool(_mutex_handle) and ctypes.get_last_error() != 183


def launcher_path() -> Path | None:
    for argument in sys.argv[1:]:
        if not argument.startswith("--launcher-b64="):
            continue
        try:
            launcher = base64.b64decode(
                argument.split("=", 1)[1], validate=True
            ).decode("utf-8")
            if launcher:
                return Path(launcher).resolve()
        except (ValueError, UnicodeDecodeError):
            pass
    return None


def executable_command() -> str:
    launcher = launcher_path()
    if launcher:
        return f'"{launcher}"'
    return f'"{Path(sys.executable).resolve()}"'


def confirm_dialog(title: str, message: str) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    message_box = user32.MessageBoxW
    message_box.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    )
    message_box.restype = ctypes.c_int
    result = message_box(
        None,
        message,
        title,
        0x00000004 |  # MB_YESNO
        0x00000040 |  # MB_ICONINFORMATION
        0x00010000,   # MB_SETFOREGROUND
    )
    if result == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return result == 6


def error_dialog(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    message_box = user32.MessageBoxW
    message_box.argtypes = (
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.UINT,
    )
    message_box.restype = ctypes.c_int
    message_box(
        None,
        message,
        title,
        0x00000000 |  # MB_OK
        0x00000010 |  # MB_ICONERROR
        0x00002000 |  # MB_TASKMODAL
        0x00010000,   # MB_SETFOREGROUND
    )


def is_autostart_enabled() -> bool:
    if not getattr(sys, "frozen", False):
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, APP_NAME)
        return value.strip() == executable_command()
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Autostart is available in the packaged EXE")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            winreg.SetValueEx(
                key, APP_NAME, 0, winreg.REG_SZ, executable_command()
            )
        else:
            with suppress_os_error():
                winreg.DeleteValue(key, APP_NAME)


class suppress_os_error:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, _traceback):
        return exc_type is not None and issubclass(exc_type, OSError)


def icon_asset_path() -> Path:
    bundle_root = Path(
        getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)
    )
    return bundle_root / "assets" / ICON_ASSET_NAME


def create_icon_image() -> Image.Image:
    with Image.open(icon_asset_path()) as source:
        return source.convert("RGBA").resize(
            (64, 64), Image.Resampling.LANCZOS
        )


class TrayService:
    def __init__(self, test_build: bool = False) -> None:
        self.test_build = test_build
        self.app_title = "Codex Meter Sync Test" if test_build else "Codex Meter Sync"
        self.stop_event = threading.Event()
        self.sync_now_event = threading.Event()
        self.test_mode_event = threading.Event()
        self.test_restore_event = threading.Event()
        self.device_change_event = threading.Event()
        self.firmware_update_queue: queue.Queue[
            update_manager.FirmwareUpdateRequest
        ] = queue.Queue(maxsize=1)
        self.update_display_queue: queue.Queue[
            update_manager.DeviceUpdateDisplayRequest
        ] = queue.Queue(maxsize=1)
        self.status = "Starting..."
        self.status_lock = threading.Lock()
        self.update_lock = threading.Lock()
        self.update_release: update_manager.UpdateRelease | None = None
        self.device_info: dict[str, object] = {
            "version": "", "ota": None, "legacy": False
        }
        self.update_check_in_progress = False
        self.client_update_in_progress = False
        self.firmware_update_in_progress = False
        self.firmware_update_phase = FIRMWARE_PHASE_IDLE
        self.state_dir = meter_sync.state_directory()
        self.skin_queue = queue.Queue(maxsize=1)
        self.skin_lock = threading.Lock()
        self.skin_in_progress = False
        self.skin_library = self.state_dir / "skins"
        self.skins = {}
        self.active_skin_sha = ""
        self.skin_status = "设备皮肤：未读取"
        if self.skin_library.exists():
            for path in self.skin_library.glob("*.meterskin"):
                try:
                    skin = skin_manager.load_package(path)
                    self.skins[skin.sha256] = skin
                except (OSError, ValueError):
                    pass
        self.log_path = sync_log_path(self.state_dir)
        try:
            prune_sync_logs(self.state_dir)
        except OSError:
            pass
        self.source_path = self.state_dir / ".source-selection.json"
        self.legacy_model_path = self.state_dir / ".model-selection.json"
        self.device_address_path = self.state_dir / ".device-address"
        self.device_name_path = self.state_dir / ".device-name"
        self.pending_firmware_path = (
            self.state_dir / ".pending-firmware-update.json"
        )
        self.device_lock = threading.Lock()
        self.discovered_devices: dict[str, str] = {}
        self.device_scan_in_progress = False
        self.args = meter_sync.parse_args([])
        self.args.source = self._load_source_selection()
        self.args.address = self._load_device_address()
        self.bound_address = self.args.address
        self.bound_name = self._load_device_name(self.bound_address)
        self.worker_lock = threading.Lock()
        self.worker = threading.Thread(target=self._worker_main, daemon=True)
        self.update_worker = threading.Thread(
            target=self._background_update_loop, daemon=True
        )
        menu_items = [
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.MenuItem("Sync now", self._sync_now, default=True),
            pystray.MenuItem("Device", pystray.Menu(self._device_menu_items)),
            pystray.MenuItem("AI Tool", self._source_menu()),
            pystray.MenuItem("皮肤", pystray.Menu(self._skin_menu_items)),
        ]
        if test_build:
            menu_items.append(pystray.MenuItem(
                "Test mode (tap device to advance)",
                self._toggle_test_mode,
                checked=lambda _item: self.test_mode_event.is_set(),
            ))
        else:
            menu_items.extend([
                pystray.MenuItem(
                    "Updates", pystray.Menu(self._update_menu_items)
                ),
                pystray.MenuItem(
                    "Start with Windows",
                    self._toggle_autostart,
                    checked=lambda _item: is_autostart_enabled(),
                ),
            ])
        menu_items.extend([
            pystray.MenuItem("Open log", self._open_log),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._exit),
        ])
        self.icon = pystray.Icon(
            "CodexMeterSync-Test" if test_build else APP_NAME,
            create_icon_image(), self.app_title,
            menu=pystray.Menu(*menu_items),
        )

    def _load_device_address(self) -> str | None:
        try:
            return self.device_address_path.read_text(encoding="utf-8").strip() or None
        except OSError:
            return None

    def _load_device_name(self, address: str | None) -> str:
        if not address:
            return ""
        try:
            saved = self.device_name_path.read_text(encoding="utf-8").strip()
        except OSError:
            saved = ""
        return meter_sync.device_display_name(saved, address)

    def _is_bound_to(self, address: str) -> bool:
        with self.device_lock:
            return self.bound_address == address

    def _device_menu_items(self):
        with self.device_lock:
            bound_address = self.bound_address
            bound_name = self.bound_name
            discovered = sorted(
                self.discovered_devices.items(), key=lambda item: (item[1], item[0])
            )
            scanning = self.device_scan_in_progress

        items = [
            pystray.MenuItem(
                f"Bound: {bound_name}" if bound_address else "Not bound",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
        ]

        def select_action(address: str, name: str):
            def action(_icon, _item) -> None:
                self._bind_device(address, name)
            return action

        for address, name in discovered:
            items.append(pystray.MenuItem(
                f"{name}  [{address}]",
                select_action(address, name),
                checked=lambda _item, candidate=address: self._is_bound_to(candidate),
                radio=True,
            ))
        if discovered:
            items.append(pystray.Menu.SEPARATOR)
        items.extend([
            pystray.MenuItem(
                "Scanning..." if scanning else "Scan for devices",
                self._scan_for_devices,
                enabled=not scanning,
            ),
            pystray.MenuItem(
                "Forget device",
                self._forget_device,
                enabled=bool(bound_address),
            ),
        ])
        return tuple(items)

    def _update_device_menu(self) -> None:
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _devices_discovered(self, devices: list[dict[str, str]]) -> None:
        with self.device_lock:
            self.discovered_devices = {
                device["address"]: meter_sync.device_display_name(
                    device.get("name"), device["address"]
                )
                for device in devices
                if device.get("address")
            }
            if self.bound_address in self.discovered_devices:
                self.bound_name = self.discovered_devices[self.bound_address]
        self._update_device_menu()

    def _binding_updated(self, address: str, name: str) -> None:
        display_name = meter_sync.device_display_name(name, address)
        with self.device_lock:
            self.bound_address = address
            self.bound_name = display_name
            self.discovered_devices[address] = display_name
        self.args.address = address
        self._reset_skin_device()
        self._update_device_menu()

    def _device_info_updated(self, info: dict[str, object]) -> None:
        if hasattr(self, "skin_lock"):
            with self.skin_lock:
                state = info.get("skin_status")
                self.active_skin_sha = str(state.get("sha256") or "") if isinstance(state, dict) else ""
                current = self.skins.get(self.active_skin_sha)
                self.skin_status = "设备皮肤：" + (current.name if current else
                    str(state.get("id") or "未读取") if isinstance(state, dict) else "未读取")
        with self.update_lock:
            self.device_info = dict(info)
        info_error = str(info.get("error") or "")
        if info_error:
            if info.get("ota") is True:
                self._set_status(
                    "Device version read failed; OTA transport detected"
                )
            else:
                self._set_status(
                    f"Device update capability unavailable: {info_error}"
                )
        device_version = str(info.get("version") or "")
        try:
            pending = json.loads(
                self.pending_firmware_path.read_text(encoding="utf-8")
            )
            target_version = str(pending.get("version") or "")
            if device_version and target_version:
                with self.update_lock:
                    phase = getattr(
                        self, "firmware_update_phase", FIRMWARE_PHASE_IDLE
                    )
                if (
                    device_version != target_version
                    and phase in FIRMWARE_TRANSFER_ACTIVE_PHASES
                ):
                    # The sync loop may reconnect and read the old running
                    # version after an update is queued but before, or while,
                    # its bytes are being transferred. That is not a rollback
                    # and must not unlock a second update attempt.
                    self._update_device_menu()
                    return
                self.pending_firmware_path.unlink(missing_ok=True)
                with self.update_lock:
                    self.firmware_update_in_progress = False
                    self.firmware_update_phase = FIRMWARE_PHASE_IDLE
                if device_version == target_version:
                    self._queue_device_update_state("IDLE")
                    self._set_status(
                        f"Device firmware {device_version} installed"
                    )
                else:
                    self._queue_device_update_state("ROLLED BACK")
                    self._set_status(
                        f"Device rolled back to firmware {device_version}"
                    )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
        self._update_device_menu()

    def _queue_device_update_state(
        self, state: str, progress: int = 0
    ) -> None:
        request = update_manager.DeviceUpdateDisplayRequest(
            state=state,
            progress=max(0, min(100, int(progress))),
        )
        while True:
            try:
                self.update_display_queue.get_nowait()
                self.update_display_queue.task_done()
            except queue.Empty:
                break
        try:
            self.update_display_queue.put_nowait(request)
        except queue.Full:
            pass

    def _scan_for_devices(self, _icon, _item) -> None:
        with self.device_lock:
            if self.device_scan_in_progress:
                return
            self.device_scan_in_progress = True
        self._set_status("Scanning for Codex Meters...")
        self._update_device_menu()
        threading.Thread(target=self._scan_device_worker, daemon=True).start()

    def _scan_device_worker(self) -> None:
        try:
            devices = asyncio.run(meter_sync.discover_devices(5.0))
            self._devices_discovered(devices)
            if devices:
                self._set_status(
                    f"Found {len(devices)} device{'s' if len(devices) != 1 else ''}; "
                    "choose one from Device"
                )
            else:
                self._set_status("No Codex Meter found")
        except Exception as exc:
            self._set_status(f"Device scan failed: {exc}")
        finally:
            with self.device_lock:
                self.device_scan_in_progress = False
            self._update_device_menu()

    def _bind_device(self, address: str, name: str) -> None:
        display_name = meter_sync.device_display_name(name, address)
        try:
            self.device_address_path.write_text(address, encoding="utf-8")
            self.device_name_path.write_text(display_name, encoding="utf-8")
        except OSError as exc:
            self._set_status(f"Could not save device binding: {exc}")
            return
        with self.device_lock:
            self.bound_address = address
            self.bound_name = display_name
            self.discovered_devices[address] = display_name
        self.args.address = address
        self.device_change_event.set()
        self._set_status(f"Switching to {display_name}")
        self._update_device_menu()

    def _forget_device(self, _icon, _item) -> None:
        try:
            self.device_address_path.unlink(missing_ok=True)
            self.device_name_path.unlink(missing_ok=True)
        except OSError as exc:
            self._set_status(f"Could not forget device: {exc}")
            return
        with self.device_lock:
            self.bound_address = None
            self.bound_name = ""
        self.args.address = None
        self._reset_skin_device()
        self.device_change_event.set()
        self._set_status("Device forgotten; scanning for a replacement")
        self._update_device_menu()

    def _load_source_selection(self) -> str:
        for path, key in (
            (self.source_path, "source"),
            (self.legacy_model_path, "model"),
        ):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return source_registry.normalize_source_id(data.get(key))
            except (OSError, AttributeError, json.JSONDecodeError, TypeError):
                pass
        return source_registry.normalize_source_id(self.args.source)

    def _source_menu(self) -> pystray.Menu:
        def action_for(source_id: str):
            def action(_icon, _item) -> None:
                self._select_source(source_id)
            return action

        def checked_for(source_id: str):
            def checked(_item) -> bool:
                return self.args.source == source_id
            return checked

        items = []
        for source_id, label in source_registry.selector_options():
            items.append(pystray.MenuItem(
                label,
                action_for(source_id),
                checked=checked_for(source_id),
                radio=True,
            ))
        return pystray.Menu(*items)

    def _select_source(self, source_id: str) -> None:
        self.args.source = source_registry.normalize_source_id(source_id)
        try:
            self.source_path.write_text(
                json.dumps({"source": self.args.source}), encoding="utf-8"
            )
        except OSError:
            pass
        self.sync_now_event.set()
        self._set_status(
            f"AI tool changed: {source_registry.selector_label(self.args.source)}"
        )

    def _update_menu_items(self):
        with self.update_lock:
            release = self.update_release
            info = dict(self.device_info)
            checking = self.update_check_in_progress
            client_updating = self.client_update_in_progress
            firmware_updating = self.firmware_update_in_progress
        device_version = str(info.get("version") or "")
        ota_state = info.get("ota")
        ota_capable = ota_state is True
        client_available = bool(
            release and update_manager.is_newer(
                release.client.version, release_config.APP_VERSION
            )
        )
        firmware_available = bool(
            release and ota_capable
            and not update_manager.is_newer(
                release.firmware.min_client, release_config.APP_VERSION
            )
            and (
                not device_version
                or update_manager.is_newer(
                    release.firmware.version, device_version
                )
            )
        )
        if ota_state is None:
            device_label = "checking update support..."
        elif device_version:
            device_label = device_version
        elif ota_capable:
            device_label = "version unknown (OTA ready)"
        else:
            device_label = "USB bootstrap required"
        return (
            pystray.MenuItem(
                f"Client: {release_config.APP_VERSION}", None, enabled=False
            ),
            pystray.MenuItem(
                f"Device: {device_label}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Checking..." if checking else "Check for updates",
                self._check_for_updates,
                enabled=(
                    not checking
                    and not client_updating
                    and not firmware_updating
                ),
            ),
            pystray.MenuItem(
                (
                    "Client update in progress..."
                    if client_updating
                    else f"Install client {release.client.version}"
                    if release and client_available
                    else "Client is up to date"
                ),
                self._install_client_update,
                enabled=client_available and not client_updating,
            ),
            pystray.MenuItem(
                (
                    f"Update device to {release.firmware.version}"
                    if release and firmware_available and not firmware_updating
                    else "Device firmware update in progress..."
                    if firmware_updating
                    else (
                        "Device requires USB OTA bootstrap"
                        if ota_state is False
                        else (
                            "Waiting for device information"
                            if ota_state is None
                            else (
                                "Update the client before device firmware"
                                if release and update_manager.is_newer(
                                    release.firmware.min_client,
                                    release_config.APP_VERSION,
                                )
                                else "Device firmware is up to date"
                            )
                        )
                    )
                ),
                self._install_firmware_update,
                enabled=(
                    firmware_available
                    and not firmware_updating
                    and self.firmware_update_queue.empty()
                ),
            ),
        )

    def _check_for_updates(self, _icon, _item) -> None:
        self._start_update_check(manual=True)

    def _start_update_check(self, manual: bool) -> None:
        with self.update_lock:
            if self.update_check_in_progress:
                return
            self.update_check_in_progress = True
        self._update_device_menu()
        threading.Thread(
            target=self._check_updates_worker,
            args=(manual,),
            daemon=True,
        ).start()

    def _check_updates_worker(self, manual: bool) -> None:
        try:
            release = update_manager.fetch_latest_release()
            with self.update_lock:
                self.update_release = release
                info = dict(self.device_info)
                firmware_updating = self.firmware_update_in_progress
            if firmware_updating:
                # Never replace an active INSTALLING/VERIFYING state with a
                # fresh UPDATE AVAILABLE result. More importantly, keep the
                # user from being prompted into a second overlapping transfer.
                return
            client_new = update_manager.is_newer(
                release.client.version, release_config.APP_VERSION
            )
            device_version = str(info.get("version") or "")
            firmware_new = bool(
                info.get("ota") is True
                and (
                    not device_version
                    or update_manager.is_newer(
                        release.firmware.version, device_version
                    )
                )
            )
            if client_new or firmware_new:
                self._queue_device_update_state("UPDATE AVAILABLE")
            if client_new:
                self._set_status(
                    f"Client update available: {release.client.version}"
                )
            elif firmware_new:
                self._set_status(
                    f"Device firmware update available: "
                    f"{release.firmware.version}"
                )
            elif manual and info.get("ota") is False:
                self._set_status(
                    "Client is current; device needs the one-time USB OTA bootstrap"
                )
            elif manual and info.get("ota") is None:
                self._set_status(
                    "Client is current; waiting for device update information"
                )
            elif manual:
                self._set_status("Client and device firmware are up to date")
        except update_manager.NoPublishedRelease as exc:
            if manual:
                self._set_status(str(exc))
        except Exception as exc:
            if manual:
                self._set_status(f"Update check failed: {exc}")
        finally:
            with self.update_lock:
                self.update_check_in_progress = False
            self._update_device_menu()

    def _background_update_loop(self) -> None:
        if self.stop_event.wait(15.0):
            return
        while not self.stop_event.is_set():
            self._start_update_check(manual=False)
            if self.stop_event.wait(
                release_config.UPDATE_CHECK_INTERVAL_SECONDS
            ):
                return

    def _download_progress(
        self, label: str, device_state: str | None = None
    ):
        last_reported = -5

        def report(percent: int) -> None:
            nonlocal last_reported
            if percent >= last_reported + 5 or percent == 100:
                last_reported = percent
                self._set_status(f"{label}: {percent}%")
                if device_state:
                    self._queue_device_update_state(
                        device_state, percent
                    )

        return report

    def _install_client_update(self, _icon, _item) -> None:
        if getattr(self, "skin_in_progress", False):
            self._notify_user("请先等待皮肤传输完成")
            return
        with self.update_lock:
            release = self.update_release
            if self.client_update_in_progress:
                return
        if not release:
            self._set_status("Check for updates first")
            return
        with self.update_lock:
            if self.client_update_in_progress:
                return
            self.client_update_in_progress = True
        self._set_status("Waiting for client update confirmation")
        self._update_device_menu()
        try:
            worker = threading.Thread(
                target=self._confirm_client_update_worker,
                args=(release,),
                daemon=True,
            )
            worker.start()
        except Exception as exc:
            with self.update_lock:
                self.client_update_in_progress = False
            message = f"Could not start update confirmation: {exc}"
            self._set_status(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()

    def _confirm_client_update_worker(
        self, release: update_manager.UpdateRelease
    ) -> None:
        try:
            confirmed = confirm_dialog(
                self.app_title,
                f"Install Codex Meter Sync {release.client.version} now?\n\n"
                "Synchronization will restart automatically.",
            )
        except Exception as exc:
            with self.update_lock:
                self.client_update_in_progress = False
            message = f"Could not open update confirmation: {exc}"
            self._set_status(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()
            return
        if not confirmed:
            with self.update_lock:
                self.client_update_in_progress = False
            self._set_status("Client update cancelled")
            self._update_device_menu()
            return
        self._queue_device_update_state("DOWNLOADING", 0)
        self._set_status(
            f"Client update {release.client.version} confirmed; starting download"
        )
        self._notify_user(
            f"Downloading Codex Meter Sync {release.client.version}..."
        )
        self._install_client_update_worker(release)

    def _install_client_update_worker(
        self, release: update_manager.UpdateRelease
    ) -> None:
        try:
            target = launcher_path()
            if target is None:
                raise update_manager.UpdateError(
                    "Self-update requires starting CodexMeterSync.exe"
                )
            self._set_status(
                f"Connecting to GitHub for client {release.client.version}"
            )
            update_dir = self.state_dir / "updates" / "pending"
            client_path = update_manager.download_asset(
                release.client.asset,
                update_dir / release_config.CLIENT_ASSET_NAME,
                self._download_progress(
                    "Downloading client update", "DOWNLOADING"
                ),
            )
            self._queue_device_update_state("VERIFYING", 100)
            time.sleep(0.6)
            update_manager.launch_integrated_client_updater(
                target,
                client_path,
                target,
                release.client.asset.sha256,
            )
            self._queue_device_update_state("RESTARTING", 100)
            time.sleep(0.6)
            self._set_status("Installing client update; restarting")
            self.stop_event.set()
            self.icon.stop()
        except Exception as exc:
            with self.update_lock:
                self.client_update_in_progress = False
            self._queue_device_update_state("UPDATE FAILED")
            message = f"Client update failed: {exc}"
            self._set_status(message)
            self._notify_user(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()

    def _install_firmware_update(self, _icon, _item) -> None:
        if getattr(self, "skin_in_progress", False):
            self._notify_user("请先等待皮肤传输完成")
            return
        with self.update_lock:
            release = self.update_release
            info = dict(self.device_info)
            if self.firmware_update_in_progress:
                return
        if not release:
            self._set_status("Check for updates first")
            return
        if info.get("ota") is not True:
            self._set_status(
                "Waiting for OTA-capable device information"
                if info.get("ota") is None
                else "This device needs the one-time USB OTA bootstrap"
            )
            return
        with self.update_lock:
            if self.firmware_update_in_progress:
                return
            self.firmware_update_in_progress = True
            self.firmware_update_phase = FIRMWARE_PHASE_CONFIRMING
        self._set_status("Waiting for firmware update confirmation")
        self._update_device_menu()
        try:
            worker = threading.Thread(
                target=self._confirm_firmware_update_worker,
                args=(release,),
                daemon=True,
            )
            worker.start()
        except Exception as exc:
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            message = f"Could not start firmware update confirmation: {exc}"
            self._set_status(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()

    def _confirm_firmware_update_worker(
        self, release: update_manager.UpdateRelease
    ) -> None:
        try:
            confirmed = confirm_dialog(
                self.app_title,
                f"Update the device to firmware {release.firmware.version}?\n\n"
                "Keep the device powered on and near this computer.",
            )
        except Exception as exc:
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            message = f"Could not open firmware confirmation: {exc}"
            self._set_status(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()
            return
        if not confirmed:
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            self._set_status("Device firmware update cancelled")
            self._update_device_menu()
            return
        self._queue_device_update_state("DOWNLOADING", 0)
        with self.update_lock:
            self.firmware_update_phase = FIRMWARE_PHASE_DOWNLOADING
        self._set_status(
            f"Firmware update {release.firmware.version} confirmed; "
            "starting download"
        )
        self._notify_user(
            f"Downloading device firmware {release.firmware.version}..."
        )
        self._install_firmware_update_worker(release)

    def _install_firmware_update_worker(
        self, release: update_manager.UpdateRelease
    ) -> None:
        try:
            self._set_status(
                f"Connecting to GitHub for firmware "
                f"{release.firmware.version}"
            )
            update_dir = self.state_dir / "updates" / "pending"
            path = update_manager.download_asset(
                release.firmware.asset,
                update_dir / release_config.FIRMWARE_OTA_ASSET_NAME,
                self._download_progress(
                    "Downloading device firmware", "DOWNLOADING"
                ),
            )
            request = update_manager.FirmwareUpdateRequest(
                path=path,
                version=release.firmware.version,
                sha256=release.firmware.asset.sha256,
                size=path.stat().st_size,
            )
            self.pending_firmware_path.write_text(
                json.dumps({
                    "version": release.firmware.version,
                    "created_at": time.time(),
                }),
                encoding="utf-8",
            )
            self._queue_device_update_state("INSTALLING", 0)
            self.firmware_update_queue.put_nowait(request)
            with self.update_lock:
                self.firmware_update_phase = FIRMWARE_PHASE_QUEUED
            self._set_status("Firmware update queued; preparing device")
            self._update_device_menu()
        except queue.Full:
            self.pending_firmware_path.unlink(missing_ok=True)
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            message = "A firmware update is already queued"
            self._set_status(message)
            self._notify_user(message)
            self._update_device_menu()
        except Exception as exc:
            self.pending_firmware_path.unlink(missing_ok=True)
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            self._queue_device_update_state("UPDATE FAILED")
            message = f"Firmware update failed: {exc}"
            self._set_status(message)
            self._notify_user(message)
            error_dialog(self.app_title, message)
            self._update_device_menu()

    def _status_text(self, _item) -> str:
        with self.status_lock:
            return self.status[:72]

    def _skin_menu_items(self):
        with self.skin_lock:
            busy = self.skin_in_progress
            skins = list(self.skins.values())
            label = self.skin_status
            active_sha = self.active_skin_sha
        yield pystray.MenuItem(label, None, enabled=False)
        yield pystray.MenuItem("导入皮肤包…", self._import_skin, enabled=not busy)
        yield pystray.MenuItem("读取设备当前皮肤", lambda _icon, _item: self._queue_skin(None, read_only=True), enabled=not busy)
        yield pystray.MenuItem("恢复内置默认皮肤", lambda _icon, _item: self._queue_skin(None), enabled=not busy)
        def item_for(skin):
            def action(_icon, _item):
                self._queue_skin(skin)
            def checked(_item):
                return skin.sha256 == active_sha
            return pystray.MenuItem(f"{skin.name} {skin.version}", action, checked=checked, enabled=not busy)
        for skin in sorted(skins, key=lambda item: (item.name, item.version, item.sha256)):
            yield item_for(skin)

    def _import_skin(self, _icon, _item):
        with self.skin_lock:
            if self.skin_in_progress:
                return
            self.skin_in_progress = True
        def worker():
            imported = None
            try:
                path = skin_manager.choose_file()
                if path is not None:
                    imported = skin_manager.import_package(path, self.skin_library)
                    with self.skin_lock:
                        self.skins[imported.sha256] = imported
                    self._set_status(f"皮肤已导入：{imported.name}")
            except Exception as exc:
                error_dialog("皮肤导入失败", str(exc))
            finally:
                with self.skin_lock:
                    self.skin_in_progress = False
                self._update_device_menu()
            if imported and confirm_dialog("应用皮肤", f"已导入 {imported.name}。现在应用到当前绑定设备吗？"):
                self._queue_skin(imported)
        threading.Thread(target=worker, daemon=True).start()

    def _reset_skin_device(self):
        if hasattr(self, "skin_lock"):
            with self.skin_lock:
                self.active_skin_sha = ""
                self.skin_status = "设备皮肤：未读取"
        with self.update_lock:
            self.device_info.pop("skin", None)

    def _queue_skin(self, skin, read_only=False):
        with self.update_lock:
            updating = self.firmware_update_in_progress or self.client_update_in_progress
            supported = self.device_info.get("skin") == 1
        if updating:
            error_dialog("暂时无法换肤", "请等待客户端或固件更新完成。")
            return
        if not self.bound_address or not supported:
            error_dialog("需要更新设备", "请先连接设备，并安装支持 Skin v1 的固件。皮肤包已保存在客户端。")
            return
        address = self.bound_address
        with self.skin_lock:
            if self.skin_in_progress:
                return
            self.skin_in_progress = True
            self.skin_status = "皮肤：等待读取" if read_only else "皮肤：等待传输"
        def complete(result, error):
            with self.skin_lock:
                self.skin_in_progress = False
                if result is not None and self.bound_address == address:
                    self.active_skin_sha = str(result.get("sha256") or "")
                    actual = self.skins.get(self.active_skin_sha)
                    self.skin_status = "设备皮肤：" + (actual.name if actual else
                        "内置默认" if result.get("id") == "builtin" else str(result.get("id") or "未读取"))
                elif error:
                    self.active_skin_sha = ""
                    self.skin_status = "设备皮肤：请重新读取确认"
            if error:
                self._set_status(f"皮肤切换失败：{error}")
                self._notify_user(f"皮肤切换未确认：{error}")
            else:
                self._set_status("已读取设备当前皮肤" if read_only else "皮肤已切换，设备已确认")
                if not read_only:
                    self._notify_user("皮肤已切换")
            self._update_device_menu()
        try:
            self.skin_queue.put_nowait(skin_manager.SkinRequest(skin, address, complete, read_only))
            self.sync_now_event.set()
        except queue.Full:
            complete(None, "已有皮肤任务等待处理")

    def _set_status(self, message: str) -> None:
        if message.startswith("Firmware update started:"):
            with self.update_lock:
                self.firmware_update_phase = FIRMWARE_PHASE_TRANSFERRING
        elif (
            message.startswith("Firmware ")
            and " transferred;" in message
        ):
            with self.update_lock:
                self.firmware_update_phase = (
                    FIRMWARE_PHASE_AWAITING_VERIFICATION
                )
        if message.startswith("Firmware update failed"):
            with self.update_lock:
                self.firmware_update_in_progress = False
                self.firmware_update_phase = FIRMWARE_PHASE_IDLE
            self.pending_firmware_path.unlink(missing_ok=True)
            self._queue_device_update_state("UPDATE FAILED")
        display_message = message
        if message.startswith("[") and "] " in message:
            display_message = message.split("] ", 1)[1]
        with self.status_lock:
            self.status = display_message
        # Logging is diagnostic only. A locked file or a temporarily unavailable
        # profile directory must never stop BLE synchronization.
        try:
            now = datetime.now().astimezone()
            stamp = now.strftime("%Y-%m-%d %H:%M:%S")
            line = (
                message if message.startswith("[")
                else f"[{stamp}] {message}"
            )
            self.log_path = append_sync_log(self.state_dir, line, now)
        except OSError:
            pass
        try:
            self.icon.title = f"{self.app_title} - {display_message[:48]}"
            self.icon.update_menu()
        except Exception:
            # The worker can report status just before/after the tray backend runs.
            pass

    def _notify_user(self, message: str) -> None:
        try:
            self.icon.notify(message, self.app_title)
        except Exception:
            # Notifications may be disabled by Windows or unavailable before
            # the tray backend is fully initialized.
            pass

    def _worker_main(self) -> None:
        while not self.stop_event.is_set():
            try:
                asyncio.run(
                    meter_sync.run(
                        self.args,
                        status_callback=self._set_status,
                        stop_event=self.stop_event,
                        sync_now_event=self.sync_now_event,
                        test_mode_event=self.test_mode_event,
                        test_restore_event=self.test_restore_event,
                        device_change_event=self.device_change_event,
                        device_list_callback=self._devices_discovered,
                        binding_callback=self._binding_updated,
                        device_info_callback=self._device_info_updated,
                        skin_queue=self.skin_queue,
                        firmware_update_queue=(
                            None if self.test_build
                            else self.firmware_update_queue
                        ),
                        update_display_queue=(
                            None if self.test_build
                            else self.update_display_queue
                        ),
                    )
                )
                if not self.stop_event.is_set():
                    self._set_status("Sync worker stopped; restarting")
            except Exception as exc:
                if not self.stop_event.is_set():
                    self._set_status(f"Sync worker error; restarting: {exc}")
            if not self.stop_event.wait(2.0):
                time.sleep(0)

    def _sync_now(self, _icon, _item) -> None:
        self.sync_now_event.set()
        recovered = False
        with self.worker_lock:
            if (
                not self.client_update_in_progress
                and self.stop_event.is_set()
            ):
                # A sync request is only reachable while the tray UI is still
                # alive. Recover from a stale stop flag left by an unexpected
                # worker shutdown instead of leaving a responsive but inert
                # tray icon behind.
                self.stop_event.clear()
                recovered = True
            if (
                not self.client_update_in_progress
                and not self.worker.is_alive()
            ):
                self.worker = threading.Thread(
                    target=self._worker_main, daemon=True
                )
                self.worker.start()
                recovered = True
        self._set_status(
            "Test display reset requested" if self.test_mode_event.is_set()
            else (
                "Sync worker restarted"
                if recovered
                else "Manual sync requested"
            )
        )

    def _toggle_test_mode(self, _icon, _item) -> None:
        if self.test_mode_event.is_set():
            self.test_mode_event.clear()
            self.sync_now_event.set()
            self._set_status("Test mode disabled; restoring live data")
        else:
            self.test_restore_event.clear()
            self.test_mode_event.set()
            self.sync_now_event.set()
            self._set_status("Test mode requested; tap device to advance")

    def _toggle_autostart(self, _icon, _item) -> None:
        try:
            enabled = not is_autostart_enabled()
            set_autostart(enabled)
            self._set_status(
                "Start with Windows enabled" if enabled
                else "Start with Windows disabled"
            )
        except Exception as exc:
            self._set_status(f"Autostart error: {exc}")

    def _open_log(self, _icon, _item) -> None:
        now = datetime.now().astimezone()
        prune_sync_logs(self.state_dir, now)
        self.log_path = sync_log_path(self.state_dir, now)
        self.log_path.touch(exist_ok=True)
        os.startfile(self.log_path)

    def _exit(self, _icon, _item) -> None:
        if self.test_mode_event.is_set():
            self.test_mode_event.clear()
            self.test_restore_event.clear()
            self.sync_now_event.set()
            self._set_status("Restoring live data before exit")
            self.test_restore_event.wait(timeout=3.0)
        self.stop_event.set()
        self.icon.stop()

    def run(self) -> None:
        self.worker.start()
        if not self.test_build:
            self.update_worker.start()
        self.icon.run()
        self.stop_event.set()
        self.worker.join(timeout=8)
        if not self.test_build and self.update_worker.is_alive():
            self.update_worker.join(timeout=1)


def main(test_build: bool = False) -> int:
    if "--validate-skin" in sys.argv:
        # Read-only packed-client smoke test / author utility. No tray, account,
        # autostart, instance-lock, BLE or persistent settings side effects.
        try:
            index = sys.argv.index("--validate-skin")
            skin = skin_manager.load_package(Path(sys.argv[index + 1]))
            print(json.dumps({"id": skin.id, "version": skin.version,
                              "bytes": len(skin.wire), "sha256": skin.sha256}))
            return 0
        except (IndexError, OSError, ValueError) as exc:
            print(f"Invalid skin: {exc}")
            return 2
    # The embedded worker is created with CREATE_NO_WINDOW by the native GUI
    # launcher. Keep this as a fallback when running the worker directly.
    hide_console_window()
    write_boot_diagnostic("test build starting" if test_build else "release build starting")
    if sys.platform != "win32":
        raise SystemExit("CodexMeterSync tray app requires Windows")
    if not acquire_single_instance():
        write_boot_diagnostic("another instance owns the lock")
        return 0
    write_boot_diagnostic("instance lock acquired")
    try:
        TrayService(test_build=test_build).run()
        return 0
    except Exception:
        try:
            now = datetime.now().astimezone()
            append_sync_log(
                meter_sync.state_directory(),
                f"[{now:%Y-%m-%d %H:%M:%S}] Unhandled exception\n"
                f"{traceback.format_exc()}",
                now,
            )
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
