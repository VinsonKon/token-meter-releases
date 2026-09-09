param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path (Split-Path -Parent $repoDirectory) 'analysis\skin-v1-tests' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$python = Join-Path $repoDirectory 'desktop\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { $python = (Get-Command python -ErrorAction Stop).Source }
$skinBinary = Join-Path $OutputDirectory 'codex-pixel-glow.skinbin'
& $python (Join-Path $PSScriptRoot 'pack-skin.py') (Join-Path $repoDirectory 'skins\codex-pixel-glow-1.0.0.meterskin') $skinBinary --compile
if ($LASTEXITCODE -ne 0) { throw 'Default skin compilation failed' }
$classicBinary = Join-Path $OutputDirectory 'codex-classic-glow.skinbin'
& $python (Join-Path $PSScriptRoot 'pack-skin.py') (Join-Path $repoDirectory 'skins\codex-classic-glow-1.0.0.meterskin') $classicBinary --compile
if ($LASTEXITCODE -ne 0) { throw 'Classic skin compilation failed' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) { throw 'Visual Studio C++ tools were not found.' }
$developerCommand = Join-Path $visualStudio 'Common7\Tools\VsDevCmd.bat'
$environmentLines = & $env:ComSpec /d /c "call `"$developerCommand`" -no_logo -arch=x64 && set"
if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize C++ environment.' }
foreach ($line in $environmentLines) {
    if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
}
$include = Join-Path $repoDirectory 'firmware\include'
$stubs = Join-Path $repoDirectory 'firmware\tests\host_stubs'
foreach ($test in @('skin_host','status_pixel_host','status_label_host','skin_pill_host')) {
    $source = Join-Path $repoDirectory "firmware\tests\$test.cpp"
    $executable = Join-Path $OutputDirectory "$test.exe"
    $object = Join-Path $OutputDirectory "$test.obj"
    & cl.exe /nologo /std:c++14 /O2 /EHsc /W4 "/I$include" "/I$stubs" "/Fe$executable" "/Fo$object" $source
    if ($LASTEXITCODE -ne 0) { throw "$test build failed" }
    if ($test -eq 'skin_host') { & $executable $skinBinary }
    elseif ($test -eq 'skin_pill_host') { & $executable $classicBinary }
    else { & $executable }
    if ($LASTEXITCODE -ne 0) { throw "$test failed" }
}
$source = Join-Path $repoDirectory 'firmware\tests\skin_storage_host.cpp'
$runtimeStubs = Join-Path $repoDirectory 'firmware\tests\runtime_stubs'
$jsonInclude = Join-Path $repoDirectory 'firmware\.pio\libdeps\waveshare-amoled-2_16\ArduinoJson\src'
$executable = Join-Path $OutputDirectory 'skin_storage_host.exe'
$object = Join-Path $OutputDirectory 'skin_storage_host.obj'
& cl.exe /nologo /std:c++14 /O2 /EHsc /W4 /D_CRT_SECURE_NO_WARNINGS "/I$runtimeStubs" "/I$include" "/I$jsonInclude" "/Fe$executable" "/Fo$object" $source
if ($LASTEXITCODE -ne 0) { throw 'Skin storage host build failed' }
& $executable $skinBinary
if ($LASTEXITCODE -ne 0) { throw 'Skin storage host failed' }
& $executable $skinBinary $classicBinary
if ($LASTEXITCODE -ne 0) { throw 'Cross-schema skin storage host failed' }
