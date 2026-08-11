param(
    [switch]$SkipFirmware,
    [switch]$SkipClient
)

$ErrorActionPreference = "Stop"
$root = [System.IO.Path]::GetFullPath($PSScriptRoot)
$firmware = Join-Path $root "firmware"
$desktop = Join-Path $root "desktop"
$environment = "waveshare-amoled-2_16"
$firmwareBuild = Join-Path $firmware ".pio\build\$environment"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Program,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

if (-not $SkipFirmware) {
    $pio = Get-Command pio -ErrorAction SilentlyContinue
    if (-not $pio) {
        $pio = Get-Command platformio -ErrorAction SilentlyContinue
    }
    if (-not $pio) {
        $workspacePio = [System.IO.Path]::GetFullPath(
            (Join-Path $root "..\..\work\.platformio\penv\Scripts\pio.exe")
        )
        if (Test-Path -LiteralPath $workspacePio -PathType Leaf) {
            $workspaceCore = Split-Path (Split-Path (Split-Path $workspacePio))
            $env:PLATFORMIO_CORE_DIR = $workspaceCore
            $pio = [pscustomobject]@{ Source = $workspacePio }
        }
    }
    if (-not $pio) {
        throw "PlatformIO was not found. Install PlatformIO Core, then run this script again."
    }
    Push-Location $firmware
    try {
        Invoke-Checked $pio.Source "run" "-e" $environment
    }
    finally {
        Pop-Location
    }
}

$otaSource = Join-Path $firmwareBuild "firmware.bin"
$factorySource = Join-Path $firmwareBuild "firmware.factory.bin"
if (-not (Test-Path -LiteralPath $otaSource -PathType Leaf)) {
    throw "Firmware OTA image is missing: $otaSource"
}
if (-not (Test-Path -LiteralPath $factorySource -PathType Leaf)) {
    throw "Firmware factory image is missing: $factorySource"
}
Copy-Item -LiteralPath $otaSource -Destination (Join-Path $root "firmware.bin") -Force
Copy-Item -LiteralPath $otaSource -Destination (Join-Path $root "codex-token-monitor.ota.bin") -Force
Copy-Item -LiteralPath $factorySource -Destination (Join-Path $root "firmware.factory.bin") -Force
Copy-Item -LiteralPath $factorySource -Destination (Join-Path $root "codex-token-monitor.factory.bin") -Force

if (-not $SkipClient) {
    Invoke-Checked (Join-Path $desktop "build-exe.cmd")
}

$python = Join-Path $desktop ".venv-build\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Desktop build Python is missing. Run desktop\build-exe.cmd first."
}
Invoke-Checked $python (Join-Path $desktop "make_release.py")

$packageRoot = Join-Path $root "release-package"
$packageRootFull = [System.IO.Path]::GetFullPath($packageRoot)
if (-not $packageRootFull.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe package staging path: $packageRootFull"
}
if (Test-Path -LiteralPath $packageRootFull) {
    Remove-Item -LiteralPath $packageRootFull -Recurse -Force
}
New-Item -ItemType Directory -Path $packageRootFull | Out-Null

$rootFiles = @(
    "VERSION",
    "README.md",
    "PROTOCOL.md",
    "CodexMeterSync.exe",
    "CodexMeterSync-Test.exe",
    "codex-token-monitor.factory.bin",
    "codex-token-monitor.ota.bin",
    "firmware.factory.bin",
    "firmware.bin",
    "flash-firmware.cmd",
    "flash-firmware.ps1",
    "start-sync.cmd",
    "build-release.ps1"
)
foreach ($name in $rootFiles) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $packageRootFull -Force
}

$desktopPackage = Join-Path $packageRootFull "desktop"
New-Item -ItemType Directory -Path $desktopPackage | Out-Null
Get-ChildItem -LiteralPath $desktop -File | Where-Object {
    $_.Extension -in ".py", ".cs", ".cmd", ".ps1", ".txt"
} | Copy-Item -Destination $desktopPackage -Force
$desktopAssets = Join-Path $desktop "assets"
if (Test-Path -LiteralPath $desktopAssets -PathType Container) {
    Copy-Item -LiteralPath $desktopAssets -Destination $desktopPackage -Recurse
}

$firmwarePackage = Join-Path $packageRootFull "firmware"
New-Item -ItemType Directory -Path $firmwarePackage | Out-Null
Copy-Item -LiteralPath (Join-Path $firmware "platformio.ini") -Destination $firmwarePackage
Copy-Item -LiteralPath (Join-Path $firmware "build_config.py") -Destination $firmwarePackage
Copy-Item -LiteralPath (Join-Path $firmware "partitions_ota_16mb.csv") -Destination $firmwarePackage
Copy-Item -LiteralPath (Join-Path $firmware "include") -Destination $firmwarePackage -Recurse
Copy-Item -LiteralPath (Join-Path $firmware "src") -Destination $firmwarePackage -Recurse

$zip = Join-Path $root "codex-token-monitor.zip"
Compress-Archive -Path (Join-Path $packageRootFull "*") -DestinationPath $zip -Force
Write-Host "Version: $((Get-Content -LiteralPath (Join-Path $root 'VERSION') -Raw).Trim())"
Write-Host "GitHub assets: $(Join-Path $root 'github-release')"
Write-Host "Unversioned package: $zip"
