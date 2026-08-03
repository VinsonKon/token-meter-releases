$ErrorActionPreference = "Stop"
$DesktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvDir = Join-Path $DesktopDir ".venv"

if (-not (Test-Path (Join-Path $VenvDir "Scripts\python.exe"))) {
    $Python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $Python) {
        $CodexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if (Test-Path -LiteralPath $CodexPython) { $Python = Get-Item $CodexPython }
    }
    if (-not $Python) { throw "Python 3 was not found. Run start-sync.cmd instead." }
    & $Python.FullName -m venv $VenvDir
    & (Join-Path $VenvDir "Scripts\python.exe") -m pip install -r (Join-Path $DesktopDir "requirements.txt")
}

& (Join-Path $VenvDir "Scripts\python.exe") (Join-Path $DesktopDir "sync.py") @args
