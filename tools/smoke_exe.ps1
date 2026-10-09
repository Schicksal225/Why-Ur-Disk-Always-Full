# Launch the built exe and require the real app window, not an error dialog.
# A crashed PyInstaller exe stays alive while its "Unhandled exception" box is
# open, so "process still running" alone is not a pass. A onefile build also
# re-launches itself, and the window belongs to that child process.
param([string]$Exe = "dist\PCOptimizer.exe", [int]$Seconds = 40)

$full = (Resolve-Path $Exe).Path
$before = @(Get-Process -Name PCOptimizer -ErrorAction SilentlyContinue | ForEach-Object Id)
Start-Process -FilePath $full -WorkingDirectory (Split-Path -Parent $full) | Out-Null
$deadline = (Get-Date).AddSeconds($Seconds)
$titles = @()
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds 700
    $mine = @(Get-Process -Name PCOptimizer -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id })
    $titles = @($mine | ForEach-Object { $_.MainWindowTitle } | Where-Object { $_ })
    if ($titles -contains "PC Optimizer" -or ($titles | Where-Object { $_ -like "*exception*" })) { break }
}
Get-Process -Name PCOptimizer -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } | Stop-Process -Force
Write-Output ("titles=" + ($titles -join " | "))
if ($titles -notcontains "PC Optimizer") { exit 1 }
