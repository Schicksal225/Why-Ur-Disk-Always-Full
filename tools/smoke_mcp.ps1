# Headless smoke test for the MCP bridge: feed initialize/tools/list/drives_list
# to "PCOptimizer.exe --mcp" over redirected stdio and require pure JSON replies.
# A windowed onefile exe keeps its GUI hidden here; only the protocol is checked.
param(
    [string]$Exe = "dist\PCOptimizer.exe",
    [int]$Seconds = 90,
    [switch]$AllowScan
)

$ErrorActionPreference = "Stop"
$full = (Resolve-Path $Exe).Path
$work = Join-Path $env:TEMP ("pcopt-mcp-smoke-" + [guid]::NewGuid().ToString("N"))
$inFile = Join-Path $work "requests.jsonl"
$outFile = Join-Path $work "replies.jsonl"
$errFile = Join-Path $work "stderr.txt"
New-Item -ItemType Directory -Force -Path $work | Out-Null

$requests = @(
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}',
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}',
    '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"drives_list","arguments":{}}}'
)
# Write UTF-8 without a BOM: the server treats the first line strictly as JSON.
$utf8 = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllLines($inFile, $requests, $utf8)

$proc = $null
try {
    $serverArgs = @("--mcp")
    if ($AllowScan) { $serverArgs += "--mcp-allow-scan" }
    $proc = Start-Process -FilePath $full -ArgumentList $serverArgs `
        -WorkingDirectory (Split-Path -Parent $full) `
        -RedirectStandardInput $inFile -RedirectStandardOutput $outFile -RedirectStandardError $errFile `
        -WindowStyle Hidden -PassThru
    if (-not $proc.WaitForExit($Seconds * 1000)) {
        try { $proc.Kill() } catch {}
        Write-Output "MCP smoke failed: the exe did not exit within $Seconds s."
        exit 1
    }
    $proc.Refresh()
    if ($null -ne $proc.ExitCode -and $proc.ExitCode -ne 0) {
        Write-Output "MCP smoke failed: the exe exited with code $($proc.ExitCode)."
        exit 1
    }

    $lines = @(Get-Content $outFile -Encoding UTF8 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    if ($lines.Count -lt 3) {
        Write-Output "MCP smoke failed: expected 3 JSON lines, got $($lines.Count)."
        Get-Content $errFile -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Write-Output "  stderr: $_" }
        exit 1
    }

    $replies = @()
    foreach ($line in $lines) {
        $replies += ($line | ConvertFrom-Json)
    }
    $byId = @{}
    foreach ($reply in $replies) { $byId[[string]$reply.id] = $reply }

    $init = $byId["1"]
    if ($null -eq $init -or -not $init.result.protocolVersion) {
        Write-Output "MCP smoke failed: initialize did not return a protocolVersion."
        exit 1
    }
    if ($init.result.protocolVersion -ne "2024-11-05") {
        Write-Output "MCP smoke failed: unexpected protocolVersion $($init.result.protocolVersion)."
        exit 1
    }

    $names = @($byId["2"].result.tools | ForEach-Object { $_.name })
    $required = @("drives_list", "findings_query", "cleanup_plan")
    if ($AllowScan) { $required += @("scan_start", "scan_status") }
    if (-not $AllowScan -and ($names -contains "scan_start")) {
        Write-Output "MCP smoke failed: scan tools are visible without --mcp-allow-scan."
        exit 1
    }
    foreach ($name in $required) {
        if ($names -notcontains $name) {
            Write-Output "MCP smoke failed: tool $name is missing (got: $($names -join ', '))."
            exit 1
        }
    }

    $call = $byId["3"]
    if ($null -eq $call -or $call.result.isError) {
        Write-Output "MCP smoke failed: drives_list returned an error."
        exit 1
    }
    $payload = ($call.result.content[0].text | ConvertFrom-Json)
    if ($null -eq $payload.drives) {
        Write-Output "MCP smoke failed: drives_list returned no drives array."
        exit 1
    }

    $mode = "default"
    if ($AllowScan) { $mode = "allow-scan" }
    $exitCode = "n/a"
    if ($null -ne $proc.ExitCode) { $exitCode = $proc.ExitCode }
    Write-Output ("MCP smoke ok: mode=$mode protocol=$($init.result.protocolVersion) tools=$($names.Count) drives=$($payload.drives.Count) exit=$exitCode")
} finally {
    if ($null -ne $proc -and -not $proc.HasExited) {
        try { $proc.Kill() } catch {}
    }
    Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    Remove-Item -Force (Join-Path (Split-Path -Parent $full) "mcp.log") -ErrorAction SilentlyContinue
}
