<#
  Builds Airlog for Windows:  dist\Airlog\Airlog.exe, a portable zip and (if Inno Setup is installed) an installer.

  Run from the project folder on any Windows 10/11 PC with Python 3.10+ installed
  (winget install Python.Python.3.12), or just double-click build_windows.bat.

    .\build_windows.ps1                      # full build, downloads ffmpeg
    .\build_windows.ps1 -FFmpegZip C:\x.zip  # use an ffmpeg build you already downloaded
    .\build_windows.ps1 -NoInstaller         # skip the Setup.exe
#>
param(
    [string]$FFmpegZip = "",
    [string]$FFmpegUrl = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    [switch]$NoInstaller
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest much faster
Set-Location $PSScriptRoot

function Step($t) { Write-Host "`n==> $t" -ForegroundColor Yellow }

$version = (Select-String -Path "airlog\__init__.py" -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
Write-Host "Airlog $version" -ForegroundColor Cyan

# ---------------------------------------------------------------- Python + PyInstaller
Step "Python build environment"
$py = $null
foreach ($cand in @(@("py", "-3.12"), @("py", "-3"), @("python"))) {
    if (Get-Command $cand[0] -ErrorAction SilentlyContinue) {
        $pre = @($cand | Select-Object -Skip 1)
        $v = & $cand[0] @pre -c "import sys; print(sys.version_info >= (3,10))" 2>$null
        if ($v -eq "True") { $py = $cand; break }
    }
}
if (-not $py) { throw "Python 3.10+ not found. Install it with:  winget install Python.Python.3.12" }
if (-not (Test-Path ".build\venv\Scripts\python.exe")) {
    $pre = @($py | Select-Object -Skip 1)
    & $py[0] @pre -m venv .build\venv
    if ($LASTEXITCODE) { throw "Could not create the build venv" }
}
$vpy = ".build\venv\Scripts\python.exe"
& $vpy -m pip install --quiet --upgrade pip
& $vpy -m pip install --quiet "pyinstaller>=6.6" "flask>=2.2" waitress pywin32
& $vpy -c "import tkinter" ; if ($LASTEXITCODE) { throw "This Python has no tkinter. Reinstall Python with 'tcl/tk' ticked." }

# ---------------------------------------------------------------- ffmpeg
Step "ffmpeg"
$ffdir = ".build\ffmpeg"
if (-not (Test-Path "$ffdir\bin\ffmpeg.exe")) {
    New-Item -ItemType Directory -Force -Path ".build" | Out-Null
    if (-not $FFmpegZip) {
        $FFmpegZip = ".build\ffmpeg.zip"
        $fallback = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        try {
            Write-Host "Downloading $FFmpegUrl"
            Invoke-WebRequest -Uri $FFmpegUrl -OutFile $FFmpegZip -UseBasicParsing
        } catch {
            Write-Host "  failed ($($_.Exception.Message)); trying $fallback"
            $FFmpegUrl = $fallback
            Invoke-WebRequest -Uri $FFmpegUrl -OutFile $FFmpegZip -UseBasicParsing
        }
    }
    $tmp = ".build\ffmpeg_extract"
    if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp }
    Expand-Archive -Path $FFmpegZip -DestinationPath $tmp
    $bin = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1
    if (-not $bin) { throw "ffmpeg.exe not found inside $FFmpegZip" }
    $root = $bin.Directory.Parent.FullName
    if (Test-Path $ffdir) { Remove-Item -Recurse -Force $ffdir }
    Move-Item $root $ffdir
    Remove-Item -Recurse -Force $tmp
}
foreach ($f in "ffmpeg.exe", "ffprobe.exe", "ffplay.exe") {
    if (-not (Test-Path "$ffdir\bin\$f")) { throw "$f missing from the ffmpeg build (use a 'full' or 'essentials' build)" }
}
& "$ffdir\bin\ffmpeg.exe" -hide_banner -version | Select-Object -First 1

# ---------------------------------------------------------------- PyInstaller
Step "PyInstaller"
if (Test-Path "dist\Airlog") { Remove-Item -Recurse -Force "dist\Airlog" }
& $vpy -m PyInstaller --noconfirm --clean --distpath dist --workpath .build\pyi packaging\airlog.spec
if ($LASTEXITCODE) { throw "PyInstaller failed" }

Copy-Item "$ffdir\bin\ffmpeg.exe", "$ffdir\bin\ffprobe.exe", "$ffdir\bin\ffplay.exe" "dist\Airlog\"
New-Item -ItemType Directory -Force -Path "dist\Airlog\licenses" | Out-Null
$lic = Get-ChildItem -Path $ffdir -Filter "LICENSE*" -File | Select-Object -First 1
if ($lic) { Copy-Item $lic.FullName "dist\Airlog\licenses\ffmpeg-LICENSE.txt" }
@"
Airlog bundles FFmpeg (https://ffmpeg.org) from $FFmpegUrl
FFmpeg is licensed under the GNU GPL v3 (this build). Source: https://ffmpeg.org/download.html
"@ | Set-Content "dist\Airlog\licenses\ffmpeg-NOTICE.txt"
Copy-Item README.md "dist\Airlog\README.md"

# ---------------------------------------------------------------- self-test the exe
Step "Self-test"
$report = Join-Path (Resolve-Path dist) "selftest.txt"
if (Test-Path $report) { Remove-Item $report }
Start-Process -FilePath "dist\Airlog\Airlog.exe" -ArgumentList "--selftest", "`"$report`"" -Wait
if (-not (Test-Path $report)) { throw "Self-test wrote no report - Airlog.exe failed to start" }
Get-Content $report
if (-not (Select-String -Path $report -Pattern "RESULT  OK" -Quiet)) { throw "Self-test failed (see above)" }

# ---------------------------------------------------------------- zip
Step "Portable zip"
$zip = "dist\Airlog-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\Airlog" -DestinationPath $zip
Write-Host "  $zip"

# ---------------------------------------------------------------- installer
if (-not $NoInstaller) {
    Step "Installer (Inno Setup)"
    $iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
    if (-not $iscc) {
        foreach ($c in "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe", "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") {
            if (Test-Path $c) { $iscc = $c; break }
        }
    }
    if ($iscc) {
        & $iscc /Q "/DAppVersion=$version" packaging\installer.iss
        if ($LASTEXITCODE) { throw "Inno Setup failed" }
        Write-Host "  dist\Airlog-Setup-$version.exe"
    } else {
        Write-Host "  Inno Setup not installed - skipping Setup.exe (winget install JRSoftware.InnoSetup)" -ForegroundColor DarkYellow
    }
}

Step "Done"
Get-ChildItem dist -File | Format-Table Name, @{n="MB"; e={[math]::Round($_.Length/1MB,1)}} -AutoSize
