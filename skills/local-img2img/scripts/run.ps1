param(
    [Parameter(Position=0)]
    [string]$ImagePath,

    [Parameter(Position=1)]
    [string]$UserPrompt
)

$ErrorActionPreference = 'Stop'

# --- Logging ---
$LogDir = Join-Path $env:USERPROFILE '.openvino\log'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }
$LogTimestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$LogFile = Join-Path $LogDir "img2img-client-$LogTimestamp.log"
Add-Content $LogFile "[$(Get-Date)] Log initialized."

function Write-Log($msg) { Add-Content $LogFile "[$(Get-Date)] $msg" }

Write-Log "run.ps1 started with image: $ImagePath prompt: $UserPrompt"

if (-not $ImagePath) {
    Write-Log 'No image path argument provided.'
    Write-Host 'Usage: scripts\run.ps1 "<image-path>" "<prompt>"'
    Write-Host '       scripts\run.ps1 --continue'
    exit 1
}

if ($ImagePath -eq '--continue') {
    Write-Host 'Resuming pending request ...'
} else {
    if (-not $UserPrompt) {
        Write-Log 'No prompt argument provided.'
        Write-Host 'Usage: scripts\run.ps1 "<image-path>" "<prompt>"'
        Write-Host '       scripts\run.ps1 --continue'
        exit 1
    }
    if (-not (Test-Path -LiteralPath $ImagePath -PathType Leaf)) {
        Write-Log "Image path not found: $ImagePath"
        Write-Host "ERROR: image path not found: $ImagePath"
        exit 1
    }
    $ImagePath = (Resolve-Path -LiteralPath $ImagePath).Path
    Write-Host "Received image: $ImagePath"
    Write-Host "Received prompt: $UserPrompt"
}

# --- AIPC Check ---
$PlatformExe = Join-Path $PSScriptRoot '..\bin\platform.exe'
Write-Log "Resolved PLATFORM_EXE=$PlatformExe"
if (-not (Test-Path $PlatformExe)) {
    Write-Log 'platform.exe was not found; skipping AIPC check (dev mode).'
    Write-Host 'WARN: bin\platform.exe missing; skipping AIPC check.'
} else {
    # 修复已知 bug：PowerShell 用 `& $exe` 捕获 stdout 时，在部分真 AIPC 上会得到空字符串，
    # 原实现仅凭 stdout 判断，导致真 AIPC 被误报为"非 AIPC"而 exit 1。
    # 这里同时检查退出码：仅当"退出码为 0 且 stdout 明确为非 '1'"时才判定为非 AIPC；
    # 其余情况（stdout 为空、退出码非 0）视为"无法确定"，放行并告警，避免在真 AIPC 上误杀。
    $IsAipc = ((& $PlatformExe --is-aipc 2>$null) | Out-String).Trim()
    $PlatformExitCode = $LASTEXITCODE
    Write-Log "platform --is-aipc returned '$IsAipc' (exit=$PlatformExitCode)"
    if ($IsAipc -eq '1') {
        Write-Log 'Intel AIPC platform confirmed.'
    } elseif ($PlatformExitCode -eq 0 -and $IsAipc -ne '') {
        Write-Log "platform.exe exited 0 but reported non-AIPC ('$IsAipc'); this machine is not an Intel AIPC."
        Write-Host 'This skill requires an Intel AIPC platform.'
        exit 1
    } else {
        Write-Log "AIPC check inconclusive (exit=$PlatformExitCode, output='$IsAipc'); continuing in dev mode."
        Write-Host 'WARN: Unable to verify Intel AIPC platform; continuing anyway.'
    }
}

# --- Setup paths ---
$SkillRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $SkillRoot
Write-Log "Changed working directory to $SkillRoot"

$EnvJson = Join-Path $SkillRoot 'info.json'
if (-not (Test-Path $EnvJson)) {
    Write-Log "ERROR: info.json not found at `"$EnvJson`"."
    Write-Host "ERROR: info.json not found."
    Pop-Location
    exit 1
}
$Config = Get-Content $EnvJson -Raw | ConvertFrom-Json
$VenvName = $Config.venv_name
if (-not $VenvName) {
    Write-Log "ERROR: venv_name not found in info.json."
    Write-Host "ERROR: venv_name not found in info.json."
    Pop-Location
    exit 1
}

$VenvDir = Join-Path $env:USERPROFILE ".openvino\venv\$VenvName"
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
Write-Log "Resolved VENV_PY=$VenvPy"

# --- Ensure environment ---
# install-env.ps1 is idempotent: ready venv + matching requirements.sha exits
# immediately. Still invoke it (except --continue) so a broken/missing env
# self-heals without the caller needing a separate install step.
if ($ImagePath -eq '--continue') {
    Write-Log 'Skipping scripts\install-env.ps1 for --continue.'
} else {
    Write-Log 'Running scripts\install-env.ps1.'
    & "$SkillRoot\scripts\install-env.ps1" -SkillRoot $SkillRoot
    $installExit = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
    if ($installExit -ne 0) { Pop-Location; exit $installExit }
    Write-Log 'scripts\install-env.ps1 completed successfully.'
}

Write-Host 'Python environment is ready. Launching client.py (Please be patient, it may take some time for the first use.) ...'

# --- Launch client.py ---
if ($ImagePath -eq '--continue') {
    Write-Log 'Launching scripts\client.py --continue.'
    & $VenvPy scripts\client.py --continue
} else {
    Write-Log "Launching scripts\client.py --image-path `"$ImagePath`" -i `"$UserPrompt`"."
    & $VenvPy scripts\client.py --image-path $ImagePath -i $UserPrompt
}
$exitCode = if ($null -eq $LASTEXITCODE) { 1 } else { [int]$LASTEXITCODE }
Write-Log "scripts\client.py exited with code $exitCode"
Pop-Location
exit $exitCode

