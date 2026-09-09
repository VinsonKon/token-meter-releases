param(
    [string]$OutputDirectory,
    [string]$FfmpegPath,
    [switch]$SkipPreview
)
$ErrorActionPreference = 'Stop'
$repoDirectory = Split-Path -Parent $PSScriptRoot
if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path (Split-Path -Parent $repoDirectory) 'video-review-2026-09-08\implementation-preview-v7'
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (-not (Test-Path -LiteralPath $vswhere)) { throw 'Visual Studio C++ tools were not found.' }
$visualStudio = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $visualStudio) { throw 'Visual Studio C++ tools were not found.' }
$developerCommand = Join-Path $visualStudio 'Common7\Tools\VsDevCmd.bat'
$environmentLines = & $env:ComSpec /d /c "call `"$developerCommand`" -no_logo -arch=x64 && set"
if ($LASTEXITCODE -ne 0) { throw 'Unable to initialize the Visual Studio build environment.' }
foreach ($line in $environmentLines) {
    if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process') }
}
$executable = Join-Path $OutputDirectory 'status-pixel-host.exe'
$object = Join-Path $OutputDirectory 'status-pixel-host.obj'
$source = Join-Path $repoDirectory 'firmware\tests\status_pixel_host.cpp'
$include = Join-Path $repoDirectory 'firmware\include'
& cl.exe /nologo /std:c++14 /O2 /EHsc /W4 "/I$include" "/Fe$executable" "/Fo$object" $source
if ($LASTEXITCODE -ne 0) { throw 'Status pixel host build failed.' }
& $executable
if ($LASTEXITCODE -ne 0) { throw 'Status pixel host checks failed.' }
if ($SkipPreview) { return }
if (-not $FfmpegPath) {
    $binaryDirectory = Join-Path (Split-Path -Parent $repoDirectory) '.video-review-tools\imageio_ffmpeg\binaries'
    $FfmpegPath = (Get-ChildItem -LiteralPath $binaryDirectory -Filter 'ffmpeg*.exe' | Select-Object -First 1).FullName
}
if (-not $FfmpegPath -or -not (Test-Path -LiteralPath $FfmpegPath)) { throw 'Supply -FfmpegPath to generate the preview.' }
$video = Join-Path $OutputDirectory 'status-pixel-core.mp4'
$gif = Join-Path $OutputDirectory 'status-pixel-core.gif'
$poster = Join-Path $OutputDirectory 'status-pixel-core.png'
$colorStrip = Join-Path $OutputDirectory 'thinking-color-cycle.png'
$staleStrip = Join-Path $OutputDirectory 'stale-reading-cycle.png'
$seamStrip = Join-Path $OutputDirectory 'thinking-seam-30fps.png'
$seamClip = Join-Path $OutputDirectory 'thinking-seam-window.mp4'
$approvalStrip = Join-Path $OutputDirectory 'approval-motion-cycle.png'
$metadata = & $executable --metadata | ConvertFrom-Json

# Stream raw frames directly between the two native processes. PowerShell's
# normal text pipeline would corrupt binary RGB data on Windows PowerShell.
$encoderInfo = New-Object System.Diagnostics.ProcessStartInfo
$encoderInfo.FileName = $FfmpegPath
$encoderInfo.Arguments = "-hide_banner -loglevel warning -y -f rawvideo -pixel_format rgb24 -video_size 832x400 -framerate 30 -i pipe:0 -an -c:v libx264 -crf 16 -pix_fmt yuv420p -movflags +faststart `"$video`""
$encoderInfo.UseShellExecute = $false
$encoderInfo.CreateNoWindow = $true
$encoderInfo.RedirectStandardInput = $true
$encoder = [System.Diagnostics.Process]::Start($encoderInfo)
$rendererInfo = New-Object System.Diagnostics.ProcessStartInfo
$rendererInfo.FileName = $executable
$rendererInfo.Arguments = '--preview'
$rendererInfo.UseShellExecute = $false
$rendererInfo.CreateNoWindow = $true
$rendererInfo.RedirectStandardOutput = $true
$renderer = [System.Diagnostics.Process]::Start($rendererInfo)
try {
    $renderer.StandardOutput.BaseStream.CopyTo($encoder.StandardInput.BaseStream)
    $encoder.StandardInput.Close()
    $renderer.WaitForExit()
    $encoder.WaitForExit()
    if ($renderer.ExitCode -ne 0 -or $encoder.ExitCode -ne 0) { throw 'Status pixel preview generation failed.' }
} finally {
    if (-not $renderer.HasExited) { $renderer.Kill() }
    if (-not $encoder.HasExited) { $encoder.Kill() }
    $renderer.Dispose()
    $encoder.Dispose()
}
& $FfmpegPath -hide_banner -loglevel warning -y -ss 3.2 -i $video -frames:v 1 -update 1 $poster
if ($LASTEXITCODE -ne 0) { throw 'Preview poster generation failed.' }
& $FfmpegPath -hide_banner -loglevel warning -y -i $video -vf "select='eq(n,0)+eq(n,120)+eq(n,240)+eq(n,360)',crop=208:242:0:40,tile=4x1" -frames:v 1 -update 1 $colorStrip
if ($LASTEXITCODE -ne 0) { throw 'Thinking color cycle strip generation failed.' }
$stalePhases = @(0, 100, 200, 400, 600, 800, 1000, 1100) | ForEach-Object { [int][Math]::Round($_ * $metadata.fps / 1000.0) }
$staleSelect = ($stalePhases | ForEach-Object { "eq(n,$_)" }) -join '+'
& $FfmpegPath -hide_banner -loglevel warning -y -i $video -vf "select='$staleSelect',crop=208:242:416:40,tile=8x1" -frames:v 1 -update 1 $staleStrip
if ($LASTEXITCODE -ne 0) { throw 'Stale reading-file cycle strip generation failed.' }
& $FfmpegPath -hide_banner -loglevel warning -y -i $video -vf "select='eq(n,0)+eq(n,7)+eq(n,13)+eq(n,16)',crop=208:242:208:40,tile=4x1" -frames:v 1 -update 1 $approvalStrip
if ($LASTEXITCODE -ne 0) { throw 'Approval motion cycle strip generation failed.' }
$seamFrame = [int][Math]::Ceiling($metadata.thinkingCycleMs * $metadata.fps / 1000.0)
$seamFirstFrame = $seamFrame - 3
$seamLastFrame = $seamFrame + 3
& $FfmpegPath -hide_banner -loglevel warning -y -i $video -vf "select='between(n,$seamFirstFrame,$seamLastFrame)',crop=208:242:0:40,tile=7x1" -frames:v 1 -update 1 $seamStrip
if ($LASTEXITCODE -ne 0) { throw 'Thinking seam sequence generation failed.' }
$seamStart = (($metadata.thinkingCycleMs - 600) / 1000.0).ToString('0.000', [Globalization.CultureInfo]::InvariantCulture)
& $FfmpegPath -hide_banner -loglevel warning -y -ss $seamStart -i $video -t 1.2 -vf 'crop=208:242:0:40,scale=416:484:flags=neighbor' -an -c:v libx264 -crf 16 -pix_fmt yuv420p -movflags +faststart $seamClip
if ($LASTEXITCODE -ne 0) { throw 'Thinking seam continuous clip generation failed.' }
& $FfmpegPath -hide_banner -loglevel warning -y -i $video -vf 'fps=15,split[a][b];[a]palettegen[p];[b][p]paletteuse' -loop 0 $gif
if ($LASTEXITCODE -ne 0) { throw 'Preview GIF generation failed.' }
Write-Output "Core-render preview (not a device recording): $video"
Write-Output "Lightweight GIF preview (15 FPS, not device frame rate): $gif"
Write-Output "Poster: $poster"
Write-Output "Thinking color cycle at 0/4/8/12 seconds: $colorStrip"
Write-Output "Stale reading-file light at 0/0.1/0.2/0.4/0.6/0.8/1.0/1.1 seconds: $staleStrip"
Write-Output "Thinking loop seam, seven consecutive 30 FPS frames: $seamStrip"
Write-Output "Thinking continuous seam window (+/-600ms): $seamClip"
Write-Output "Approval ring/corners/center/cross motion: $approvalStrip"
