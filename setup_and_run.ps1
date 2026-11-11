<#
  Document Request Ledger - one-shot setup and launch (Windows PowerShell).

  Creates the venv, installs requirements, makes sure .env has a real database password,
  creates the oak_request_ledger database + tables, seeds the demo engagement and starts the
  server on http://127.0.0.1:8015

  Usage:   powershell -ExecutionPolicy Bypass -File .\setup_and_run.ps1
  Options: -SkipSeed   do not run the demo seeder
           -NoRun      set everything up but do not start the server
#>
[CmdletBinding()]
param(
    [switch]$SkipSeed,
    [switch]$NoRun
)

# Native tools (pip, python) write progress/warnings to stderr; under 'Stop' Windows PowerShell 5.1
# would treat that as a fatal error. We check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = 'Continue'
Set-Location -Path $PSScriptRoot

$HostAddress = '127.0.0.1'
$Port = 8015

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Fail([string]$Message) { Write-Host "`nERROR: $Message" -ForegroundColor Red; exit 1 }

# ---------------------------------------------------------------- 1. Python ------------------
Write-Step 'Locating Python 3.10+'
$pyCmd = $null
foreach ($candidate in @('py', 'python')) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) { $pyCmd = $candidate; break }
}
if (-not $pyCmd) { Fail 'Python was not found on PATH. Install Python 3.10 or newer from python.org and re-run.' }
$pyPrefix = @()
if ($pyCmd -eq 'py') { $pyPrefix = @('-3') }

& $pyCmd @pyPrefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if ($LASTEXITCODE -ne 0) { Fail 'Python 3.10 or newer is required.' }
& $pyCmd @pyPrefix --version

# ---------------------------------------------------------------- 2. venv --------------------
$venvPython = Join-Path $PSScriptRoot 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    Write-Step 'Creating virtual environment (venv)'
    & $pyCmd @pyPrefix -m venv venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $venvPython)) { Fail 'Could not create the virtual environment.' }
} else {
    Write-Step 'Virtual environment already exists'
}

# ---------------------------------------------------------------- 3. requirements -----------
Write-Step 'Installing requirements'
& $venvPython -m pip install --disable-pip-version-check --quiet --upgrade pip
& $venvPython -m pip install --disable-pip-version-check -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail 'pip install failed - see the output above.' }

# ---------------------------------------------------------------- 4. .env ------------------
Write-Step 'Checking .env'
if (-not (Test-Path '.env')) {
    if (-not (Test-Path '.env.example')) { Fail '.env.example is missing.' }
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example'
}
$envText = Get-Content -Path '.env' -Raw
if ($envText -match '<password>') {
    $secure = Read-Host "Enter the PostgreSQL password for user 'postgres' (localhost:5432)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    if ([string]::IsNullOrEmpty($plain)) { Fail 'No password entered. Edit .env and replace <password>, then re-run.' }
    $encoded = [Uri]::EscapeDataString($plain)
    $envText = $envText.Replace('<password>', $encoded)
    [IO.File]::WriteAllText((Join-Path $PSScriptRoot '.env'), $envText, (New-Object Text.UTF8Encoding($false)))
    Write-Host 'Saved the password (URL-encoded) into .env'
}

# ---------------------------------------------------------------- 5. database --------------
Write-Step 'Creating database oak_request_ledger (if missing) and tables'
& $venvPython database.py
if ($LASTEXITCODE -ne 0) { Fail 'Database setup failed. Is PostgreSQL running, and is the password in .env correct?' }

# ---------------------------------------------------------------- 6. seed -------------------
if (-not $SkipSeed) {
    Write-Step 'Seeding the demo engagement'
    & $venvPython seed_demo.py
    if ($LASTEXITCODE -ne 0) { Fail 'Seeding failed - see the output above.' }
}

# ---------------------------------------------------------------- 7. run -------------------
if ($NoRun) {
    Write-Host "`nSetup complete. Start the server with:  venv\Scripts\python.exe -m uvicorn main:app --host $HostAddress --port $Port" -ForegroundColor Green
    exit 0
}

Write-Step "Starting the ledger on http://${HostAddress}:${Port}  (Ctrl+C to stop)"
& $venvPython -m uvicorn main:app --host $HostAddress --port $Port
