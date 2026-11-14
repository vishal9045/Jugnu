# Removes Jugnu from this computer: stops it, removes what was installed for it, and asks
# before deleting anything of yours or anything another program might share. Run uninstall.bat.
# Shared tools (uv, Python, Node.js, Claude Code, Codex, gcloud) stay: other programs use them.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $root '.venv'
$docs = [Environment]::GetFolderPath('MyDocuments')

function Ask($question) {
    return (Read-Host "$question [y/N]") -match '^\s*(y|yes)\s*$'
}
function Remove($path, $what) {
    if (-not (Test-Path -LiteralPath $path)) { return }
    try {
        Remove-Item -LiteralPath $path -Recurse -Force
        Write-Host "  removed $what"
    } catch {
        Write-Host "  couldn't remove $what ($($_.Exception.Message)); close what's using it and run this again"
    }
}

# where it kept things, from .env when it says (else the defaults)
$envFile = Join-Path $root '.env'
$settings = @{}
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Z0-9_]+)\s*=\s*(.*)$') { $settings[$Matches[1]] = $Matches[2].Trim().Trim('"') }
    }
}
$reports = if ($settings['JUGNU_REPORTS']) { $settings['JUGNU_REPORTS'] } else { Join-Path $docs 'Jugnu reports' }
$workspaces = if ($settings['JUGNU_WORKSPACES']) { $settings['JUGNU_WORKSPACES'] } else { Join-Path $docs 'Jugnu workspaces' }
$image = if ($settings['JUGNU_AUDITOR_IMAGE']) { $settings['JUGNU_AUDITOR_IMAGE'] } else { 'jugnu-auditor' }

Write-Host "Uninstalling Jugnu from $root`n"

# 1. stop this copy (only programs running from this folder's .venv; another copy keeps running)
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and $_.ExecutablePath.StartsWith($venv, [StringComparison]::OrdinalIgnoreCase) })
foreach ($p in $running) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
if ($running.Count) { Write-Host "  stopped Jugnu"; Start-Sleep -Seconds 2 }

# 2. the shortcuts install and setup made (Start menu, start at login), only the ones that start this copy
$shell = New-Object -ComObject WScript.Shell
function Mine($path) {
    if (-not (Test-Path -LiteralPath $path)) { return $false }
    $target = if ($path -like '*.lnk') { $shell.CreateShortcut($path).TargetPath } else { Get-Content -LiteralPath $path -Raw }
    return [bool]$target -and $target.ToLower().Contains($root.ToLower())
}
$startup = [Environment]::GetFolderPath('Startup')
foreach ($entry in 'Jugnu.lnk', 'Jugnu.bat') {
    $path = Join-Path $startup $entry
    if (Mine $path) { Remove $path 'the start-at-login entry' }
}
$menuLink = Join-Path ([Environment]::GetFolderPath('Programs')) 'Jugnu.lnk'
if (Mine $menuLink) { Remove $menuLink 'the Start menu entry' }
Remove (Join-Path $root 'Jugnu.lnk') 'the shortcut in this folder'

# 3. what install.bat installed here
Remove $venv 'the Python environment (.venv)'

Write-Host "`nThe rest is yours, or may be shared with another copy of Jugnu, so each is asked first:"

# 4. yours
if ((Test-Path -LiteralPath $envFile) -and (Ask "Delete .env (your settings and keys)?")) { Remove $envFile '.env' }
$logs = Join-Path $root 'logs'
if ((Test-Path -LiteralPath $logs) -and (Ask "Delete logs (your conversation history)?")) { Remove $logs 'logs' }
if ((Test-Path -LiteralPath $reports) -and (Ask "Delete the reports folder ($reports)?")) { Remove $reports 'the reports folder' }
if ((Test-Path -LiteralPath $workspaces) -and (Ask "Delete the council's workspaces ($workspaces)?")) { Remove $workspaces 'the council workspaces' }

# 5. possibly shared
$hub = Join-Path $env:USERPROFILE '.cache\huggingface\hub'
$models = @(Get-ChildItem -LiteralPath $hub -Directory -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -like 'models--ResembleAI--chatterbox*' -or $_.Name -like 'models--openai--whisper-large-v3*' })
if ($models.Count) {
    $gb = [math]::Round((($models | Get-ChildItem -Recurse -File | Measure-Object Length -Sum).Sum) / 1GB, 1)
    if (Ask "Delete the local voice and ears models ($gb GB)?") { foreach ($m in $models) { Remove $m.FullName $m.Name } }
}
$tokens = Join-Path $env:USERPROFILE '.google_workspace_mcp'
if ((Test-Path -LiteralPath $tokens) -and (Ask "Delete the saved Google Drive and Gmail sign-in?")) { Remove $tokens 'the Drive and Gmail sign-in' }
if (Get-Command docker -ErrorAction SilentlyContinue) {
    docker image inspect $image *> $null
    if ($LASTEXITCODE -eq 0 -and (Ask "Delete the council's Docker image ($image, about 10 GB)?")) {
        docker image rm $image *> $null
        if ($LASTEXITCODE -eq 0) { Write-Host "  removed the Docker image" } else { Write-Host "  couldn't remove the Docker image (is Docker Desktop running?)" }
    }
}

Write-Host @"

Done. Left in place, because other programs use them: uv, Python, Node.js, Claude Code, Codex, gcloud.
To take back Jugnu's access to your Google account: https://myaccount.google.com/connections
(the ears' Google sign-in: gcloud auth application-default revoke).
This folder now holds only Jugnu's code; delete it whenever you like.
"@
