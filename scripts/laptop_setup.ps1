# Call Kettle laptop setup (Windows). Run in PowerShell on the laptop:
#   irm https://raw.githubusercontent.com/samiali38183/callkettle/main/scripts/laptop_setup.ps1 | iex
# The repo is private, so if that URL 404s, first run `gh auth login`, then:
#   gh repo clone samiali38183/callkettle $HOME\deskline-ai ; & $HOME\deskline-ai\scripts\laptop_setup.ps1
# Safe to re-run: it only installs what is missing and pulls the latest code.
$ErrorActionPreference = 'Stop'
$repo = Join-Path $HOME 'deskline-ai'

function Need($cmd, $wingetId) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) {
    Write-Host "Installing $wingetId ..."
    winget install --id $wingetId -e --accept-source-agreements --accept-package-agreements
  }
}
Need git 'Git.Git'
Need gh 'GitHub.cli'
Need python 'Python.Python.3.12'
$env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')

gh auth status 2>$null; if ($LASTEXITCODE -ne 0) { gh auth login --web --git-protocol https }

if (Test-Path (Join-Path $repo '.git')) {
  git -C $repo pull --rebase --autostash
} else {
  gh repo clone samiali38183/callkettle $repo
}

Set-Location (Join-Path $repo 'backend')
if (-not (Test-Path '.venv')) { python -m venv .venv }
.\.venv\Scripts\python.exe -m pip install -q --upgrade pip
.\.venv\Scripts\python.exe -m pip install -q -r requirements.txt
if (Test-Path 'requirements-dev.txt') { .\.venv\Scripts\python.exe -m pip install -q -r requirements-dev.txt }

Write-Host ''
Write-Host 'Checking the copy works (tests)...'
$env:PYTHONPATH = ''
.\.venv\Scripts\python.exe -m pytest -q
Set-Location $repo
Write-Host ''
Write-Host 'DONE. Daily use on this laptop:'
Write-Host '  cd ~\deskline-ai ; git pull            # get the latest before working'
Write-Host '  backend\.venv\Scripts\python.exe marketing\sales.py today   # your call list'
Write-Host '  git add -A ; git commit -m "notes" ; git push                # send changes back'
Write-Host 'Not synced on purpose: backend\.env (secrets) and private sales logs. Copy .env by USB only if you need to deploy from the laptop.'
