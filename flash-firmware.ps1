param(
    [Parameter(Mandatory = $true)]
    [string]$Port,
    [int]$Baud = 460800
)

$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"
$firmware = Join-Path $PSScriptRoot "codex-token-monitor.factory.bin"
if (-not (Test-Path -LiteralPath $firmware)) {
    throw "Firmware image not found: $firmware"
}

$codexPython = Join-Path $env:USERPROFILE "Documents\Codex\pio\penv\Scripts\python.exe"
$codexEsptool = Join-Path $env:USERPROFILE "Documents\Codex\pio\packages\tool-esptoolpy\esptool.py"
$python = Get-Command py -ErrorAction SilentlyContinue
$pythonArgs = @("-3", "-m", "esptool")
if (-not $python) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    $pythonArgs = @("-m", "esptool")
}
if (-not $python -and (Test-Path -LiteralPath $codexPython) -and
    (Test-Path -LiteralPath $codexEsptool)) {
    $python = Get-Item -LiteralPath $codexPython
    $pythonArgs = @($codexEsptool)
}
if (-not $python) {
    throw "Python 3 was not found. Run flash-firmware.cmd instead, or install Python and esptool."
}

Write-Host "Flashing $firmware to $Port..."
& $python.FullName @pythonArgs --chip esp32s3 --port $Port --baud $Baud `
    write-flash 0x0 $firmware
if ($LASTEXITCODE -ne 0) {
    throw "Flashing failed. Hold BOOT, reconnect USB, release BOOT, and try again."
}

Write-Host "Flash complete. Reconnect or reset the board; it will advertise as Codex Meter-XXXX."
