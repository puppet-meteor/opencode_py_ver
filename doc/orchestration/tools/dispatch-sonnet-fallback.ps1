# dispatch-sonnet-fallback.ps1
# Ticket: T-C1-PT-005 | User ruling 2026-11-19 (verbatim):
#   如果sonnet不可用的时候，测试两次，仍不可用时降级为opencode GLM5.3
# Policy implemented:
#   1. Probe codex + claude-sonnet-5-5 with a real PONG round-trip (up to MaxTests times,
#      each capped by ProbeTimeoutS; wait ProbeIntervalS between failures).
#   2. Probe verdict = exit code 0 AND output contains PONG.
#      (Gateway HTTP liveness is NOT sufficient: codex rides /v1/responses.)
#   3. Any probe pass  -> dispatch the sonnet wrapper (codex session resume).
#      All probes fail -> dispatch the GLM fallback seat (py opencode CLI, glm-5.3).
# Scope: orchestration-only tool; intentionally kept outside ruff/mypy gates.
#
# KNOWN LIMITATIONS (2026-11-19 field record, PT-005):
#   - PONG-alive != heavy-load-alive: this probe passed while real resume
#     traffic (880K-tok session replay) died four times in a row on the
#     gateway (524 mid-stream / reconnect-stall freezes). A PONG verdict can
#     therefore dispatch into a known-bad gateway; the growth sentinel
#     (45-min zero-write alarm) is the backstop, not this probe.
#   - PS 5.1 traps baked into the code below, keep them when editing:
#     Start-Process joins array ArgumentList WITHOUT quoting (use one
#     pre-quoted arg string); ExitCode reads null until a parameterless
#     WaitForExit() flushes the async readers (PONG is the ground truth —
#     a missing exit code must not veto it, only a real non-zero may).
param(
    [int]$MaxTests = 2,
    [int]$ProbeTimeoutS = 240,
    [int]$ProbeIntervalS = 60,
    [string]$RepoRoot = 'D:\26_code\opencode_py_ver\opencode-2.2.2-py',
    [Parameter(Mandatory = $true)][string]$SonnetScript,
    [Parameter(Mandatory = $true)][string]$GlmScript,
    [Parameter(Mandatory = $true)][string]$LogPath,
    [string]$WorkDir = 'C:\Users\lyu\AppData\Local\Temp\opencode\dispatch'
)

$ErrorActionPreference = 'Continue'
$script:CodexExe = $null

function Log([string]$Message) {
    $line = '[{0}] {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $LogPath -Value $line -Encoding UTF8
}

function Resolve-CodexExe {
    if ($script:CodexExe) { return $script:CodexExe }
    $cmd = Get-Command codex -ErrorAction SilentlyContinue
    if ($null -ne $cmd -and $cmd.Source -and ($cmd.Source -like '*.exe')) {
        $script:CodexExe = $cmd.Source
        return $script:CodexExe
    }
    $hit = Get-ChildItem 'C:\Users\lyu\AppData\Roaming\npm\node_modules' -Recurse -Filter 'codex.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($hit) {
        $script:CodexExe = $hit.FullName
        return $script:CodexExe
    }
    return $null
}

function Kill-CodexOrphans([string]$Marker) {
    Get-CimInstance Win32_Process -Filter "Name='codex.exe' OR Name='node.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like ('*' + $Marker + '*') } |
        ForEach-Object {
            Log ('killing orphan {0} pid={1}' -f $_.Name, $_.ProcessId)
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Test-SonnetAvailable([string]$OutPrefix) {
    $outFile = "$OutPrefix-out.txt"
    $errFile = "$OutPrefix-err.txt"
    $exe = Resolve-CodexExe
    if ($exe) {
        Log ('probe via exe: {0}' -f $exe)
        # PS 5.1 Start-Process joins array ArgumentList WITHOUT quoting:
        # a space-bearing prompt must be pre-quoted in a single arg string
        # (first run died as codex usage error because of this).
        $argLine = 'exec -m claude-sonnet-5-5 "Reply with exactly: PONG"'
        $p = Start-Process -FilePath $exe -ArgumentList $argLine -WorkingDirectory $RepoRoot -RedirectStandardOutput $outFile -RedirectStandardError $errFile -PassThru -WindowStyle Hidden
    }
    else {
        Log 'probe via cmd shim: codex'
        $argStr = '/c codex exec -m claude-sonnet-5-5 "Reply with exactly: PONG" > "' + $outFile + '" 2>&1'
        $p = Start-Process -FilePath $env:ComSpec -ArgumentList $argStr -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden
    }
    if (-not $p.WaitForExit($ProbeTimeoutS * 1000)) {
        Log ('probe TIMEOUT after {0}s - killing process tree + PONG orphans' -f $ProbeTimeoutS)
        taskkill /PID $p.Id /T /F 2>$null | Out-Null
        Kill-CodexOrphans 'Reply with exactly: PONG'
        return $false
    }
    # Parameterless WaitForExit() flushes the async output readers; without it
    # ExitCode reads as null under PS 5.1 Start-Process -PassThru + redirect
    # (second run: pong=True but exit coerced to -1 -> wrongful fallback).
    $p.WaitForExit() | Out-Null
    $code = $p.ExitCode
    $codeMissing = $null -eq $code
    if ($codeMissing) { $code = -1 }
    $text = ''
    foreach ($f in @($outFile, $errFile)) {
        if (Test-Path $f) { $text += (Get-Content $f -Raw -ErrorAction SilentlyContinue) }
    }
    $pong = $false
    if ($text) { $pong = ($text -match 'PONG') }
    Log ('probe done: exit={0} (missing={1}) pong={2}' -f $code, $codeMissing, $pong)
    # PONG from the model is the ground-truth availability signal; a missing
    # exit code (PS plumbing quirk) must not veto it, only a real non-zero may.
    if ($codeMissing) { return $pong }
    return (($code -eq 0) -and $pong)
}

New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

Log ('=== dispatch-sonnet-fallback start: MaxTests={0} ProbeTimeoutS={1} ProbeIntervalS={2} ===' -f $MaxTests, $ProbeTimeoutS, $ProbeIntervalS)
Log ('repo={0}' -f $RepoRoot)
Log ('sonnetScript={0} | glmScript={1}' -f $SonnetScript, $GlmScript)

$available = $false
for ($i = 1; $i -le $MaxTests; $i++) {
    Log ('--- probe {0}/{1} ---' -f $i, $MaxTests)
    $ok = Test-SonnetAvailable -OutPrefix (Join-Path $WorkDir ('probe{0}' -f $i))
    if ($ok) { $available = $true; break }
    Log ('probe {0}/{1} FAILED' -f $i, $MaxTests)
    if ($i -lt $MaxTests) {
        Log ('sleeping {0}s before next probe' -f $ProbeIntervalS)
        Start-Sleep -Seconds $ProbeIntervalS
    }
}

if ($available) {
    Log ('SONNET AVAILABLE -> dispatch sonnet wrapper: {0}' -f $SonnetScript)
    Set-Content -Path (Join-Path $WorkDir 'branch.txt') -Value 'sonnet' -Encoding ASCII
    & powershell -NoProfile -ExecutionPolicy Bypass -File $SonnetScript
    $exitCode = $LASTEXITCODE
    Log ('sonnet wrapper finished: exit={0}' -f $exitCode)
    exit $exitCode
}
else {
    Log ('SONNET UNAVAILABLE after {0} probes -> FALLBACK to GLM seat: {1}' -f $MaxTests, $GlmScript)
    Set-Content -Path (Join-Path $WorkDir 'branch.txt') -Value 'glm' -Encoding ASCII
    & powershell -NoProfile -ExecutionPolicy Bypass -File $GlmScript
    $exitCode = $LASTEXITCODE
    Log ('glm wrapper finished: exit={0}' -f $exitCode)
    exit $exitCode
}
