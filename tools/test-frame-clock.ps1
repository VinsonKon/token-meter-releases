param([string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $taskRepo 'work\frame-clock-tests' }
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$taskVswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$taskVs = & $taskVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $taskVs) { throw 'C++ build tools unavailable' }
$taskDeveloper = Join-Path $taskVs 'Common7\Tools\VsDevCmd.bat'
$taskLines = & $env:ComSpec /d /c "call `"$taskDeveloper`" -no_logo -arch=x64 && set"
if ($LASTEXITCODE -ne 0) { throw 'C++ environment initialization failed' }
foreach ($taskLine in $taskLines) {
    if ($taskLine -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
}
$taskExe = Join-Path $OutputDirectory 'frame_clock_host.exe'
$taskObj = Join-Path $OutputDirectory 'frame_clock_host.obj'
& cl.exe /nologo /std:c++14 /O2 /EHsc /W4 "/I$(Join-Path $taskRepo 'firmware\include')" "/Fe$taskExe" "/Fo$taskObj" (Join-Path $taskRepo 'firmware\tests\frame_clock_host.cpp')
if ($LASTEXITCODE -ne 0) { throw 'Frame clock host build failed' }
& $taskExe
if ($LASTEXITCODE -ne 0) { throw 'Frame clock checks failed' }
