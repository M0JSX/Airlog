# Run by Setup before it replaces Airlog's files (upgrade or repair): stop the service and make sure
# nothing is still running from the install folder, so no file (Airlog.exe, vcruntime140.dll, ffmpeg.exe...)
# is locked. Stop-Service returns when the service reports stopped, which can be before its process has
# exited; an archive encoder may also still be finishing the current file.
param([Parameter(Mandatory = $true)][string]$Dir)
$ErrorActionPreference = 'SilentlyContinue'

Stop-Service AirlogRecorder

$prefix = $Dir.TrimEnd('\') + '\'
function Get-FromAirlog {
    Get-Process | Where-Object { $_.Path -and $_.Path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
}

# Let them finish on their own first (the service process, an archive encoder closing its file): up to 20 s.
$end = (Get-Date).AddSeconds(20)
while ((Get-FromAirlog) -and (Get-Date) -lt $end) { Start-Sleep -Milliseconds 500 }

# Anything still running from the folder (the classic desktop window, a stuck process): close it.
$left = @(Get-FromAirlog)
if ($left.Count) {
    $left | ForEach-Object { Write-Output "Closing $($_.Name) ($($_.Id)): $($_.Path)" }
    $left | Stop-Process -Force
    Start-Sleep -Milliseconds 800
}
exit 0
