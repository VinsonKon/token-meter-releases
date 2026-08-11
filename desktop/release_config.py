"""Shared release identity for GitHub-based client and firmware updates."""

APP_VERSION = "1.0.9"
RELEASE_REPOSITORY = "VinsonKon/token-meter-releases"
RELEASE_DOWNLOAD_BASE_URL = (
    f"https://github.com/{RELEASE_REPOSITORY}/releases"
)
RELEASE_API_URL = (
    f"https://api.github.com/repos/{RELEASE_REPOSITORY}/releases/latest"
)
UPDATE_CHECK_INTERVAL_SECONDS = 6 * 60 * 60

CLIENT_ASSET_NAME = "CodexMeterSync.exe"
FIRMWARE_OTA_ASSET_NAME = "codex-token-monitor.ota.bin"
FIRMWARE_FACTORY_ASSET_NAME = "codex-token-monitor.factory.bin"
MANIFEST_ASSET_NAME = "update.json"
LATEST_MANIFEST_URL = (
    f"{RELEASE_DOWNLOAD_BASE_URL}/latest/download/{MANIFEST_ASSET_NAME}"
)
