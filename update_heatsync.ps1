# HeatSync updater - run by start_heatsync.bat before the app starts.
# Checks GitHub for a newer commit on main and installs it.
#
# Exit codes: 0 = start this copy as it is, 2 = files changed (relaunch the .bat).
# Any problem falls back to starting the current copy: a failed update must
# never stop the heating controller from starting.
param([string]$LogFile)

$repo   = 'lukegardner43/HeatSync'
$branch = 'main'
$here   = $PSScriptRoot
$marker = Join-Path $here '.heatsync_version'
# Local-only files and folders an update must never touch
$keep   = @('config.json', 'heatsync.log', '.heatsync_version', 'venv', '.git')

# The progress bar slows downloads to a crawl in Windows PowerShell 5.1
$ProgressPreference = 'SilentlyContinue'
# GitHub needs TLS 1.2, which older Windows PowerShell doesn't enable by default
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

function Say($msg) {
    Write-Host $msg
    if ($LogFile) { Add-Content -Path $LogFile -Value "[UPDATE] $msg" }
}

Say 'Checking GitHub for updates...'

# A git clone is updated through git, so its history stays in step with the files
if (Test-Path (Join-Path $here '.git')) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Say 'This is a git clone but git is not installed - starting current version'
        exit 0
    }
    # Never stop to ask for a login: started at boot, nobody would see the prompt
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:GCM_INTERACTIVE = 'never'
    $before = git -C $here rev-parse HEAD
    $out = git -C $here pull --ff-only origin $branch 2>&1
    if ($LASTEXITCODE -ne 0) {
        Say "git pull failed, starting current version: $out"
        exit 0
    }
    $after = git -C $here rev-parse HEAD
    if ($after -eq $before) {
        Say "Up to date ($($after.Substring(0, 7)))"
        exit 0
    }
    Say "Updated to $($after.Substring(0, 7))"
    exit 2
}

# Otherwise this is a downloaded ZIP: compare against the commit we last installed
$ErrorActionPreference = 'Stop'
try {
    $latest = (Invoke-RestMethod -Uri "https://api.github.com/repos/$repo/commits/$branch" -UseBasicParsing -TimeoutSec 15).sha
    if (-not $latest) { throw 'GitHub did not return a commit' }
} catch {
    Say "Could not check GitHub, starting current version: $($_.Exception.Message)"
    exit 0
}

$current = ''
if (Test-Path $marker) { $current = (Get-Content $marker -Raw).Trim() }
if ($current -eq $latest) {
    Say "Up to date ($($latest.Substring(0, 7)))"
    exit 0
}

$tmp = Join-Path ([IO.Path]::GetTempPath()) ('heatsync-update-' + [guid]::NewGuid())
$code = 0
try {
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'update.zip'
    Say "Downloading update $($latest.Substring(0, 7))..."
    # Fetch the exact commit we compared against, not whatever main is by now
    Invoke-WebRequest -Uri "https://api.github.com/repos/$repo/zipball/$latest" -OutFile $zip -UseBasicParsing -TimeoutSec 120
    Expand-Archive -Path $zip -DestinationPath (Join-Path $tmp 'files')
    $src = (Get-ChildItem (Join-Path $tmp 'files') -Directory | Select-Object -First 1).FullName
    if (-not $src -or -not (Test-Path (Join-Path $src 'app.py'))) { throw 'the download did not contain HeatSync' }

    $files = Get-ChildItem $src -Recurse -File -Force | Where-Object {
        $keep -notcontains ($_.FullName.Substring($src.Length + 1) -split '[\\/]')[0]
    }
    # From here on files may have changed, so the .bat must relaunch either way
    $code = 2
    foreach ($f in $files) {
        $dest = Join-Path $here $f.FullName.Substring($src.Length + 1)
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item -LiteralPath $f.FullName -Destination $dest -Force
    }
    Set-Content -Path $marker -Value $latest
    Say "Updated to $($latest.Substring(0, 7))"
} catch {
    Say "Update failed, starting current version: $($_.Exception.Message)"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}
exit $code
