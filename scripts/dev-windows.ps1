param(
    [Parameter(Position = 0)]
    [string] $Action = "help",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RemainingArgs = @()
)

$ErrorActionPreference = "Stop"
$Root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
Set-Location -LiteralPath $Root

$EnvFile = if ($env:LIVIUM_ENV_FILE) {
    if ([IO.Path]::IsPathRooted($env:LIVIUM_ENV_FILE)) {
        [IO.Path]::GetFullPath($env:LIVIUM_ENV_FILE)
    } else {
        [IO.Path]::GetFullPath((Join-Path $Root $env:LIVIUM_ENV_FILE))
    }
} else {
    Join-Path $Root ".env"
}
$Python = Join-Path $Root ".venv\Scripts\python.exe"

function New-EnvFileIfMissing {
    if (Test-Path -LiteralPath $EnvFile -PathType Leaf) { return }
    $Parent = Split-Path -Parent $EnvFile
    if ($Parent) { New-Item -ItemType Directory -Force -Path $Parent | Out-Null }
    Copy-Item -LiteralPath (Join-Path $Root ".env.example") -Destination $EnvFile
    Write-Host "Created $EnvFile from .env.example. Add your API key, then run this command again."
}

function Import-EnvFile {
    if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
        throw "Environment file not found: $EnvFile. Run setup first."
    }
    foreach ($Line in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        if ($Line -match '^\s*(?:#|$)') { continue }
        if ($Line -notmatch '^\s*(?<name>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?<value>.*)\s*$') {
            throw "Invalid .env assignment. Check the file without sharing secret values."
        }
        $Name = $Matches.name
        $Value = $Matches.value
        if ($Value.Length -ge 2 -and
            (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
             ($Value.StartsWith("'") -and $Value.EndsWith("'")))) {
            $Value = $Value.Substring(1, $Value.Length - 2)
        }
        [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
    }
}

function Require-PythonEnvironment {
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "Virtual environment is missing. Run: .\scripts\dev-windows.ps1 setup"
    }
}

switch ($Action.ToLowerInvariant()) {
    { $_ -in @("help", "-h", "--help") } {
        @"
Livium Windows development helper

  .\scripts\dev-windows.ps1 setup       Create .venv and a private env template if absent
  .\scripts\dev-windows.ps1 doctor      Probe the configured provider and runtime
  .\scripts\dev-windows.ps1 test         Run the offline Python suite
  .\scripts\dev-windows.ps1 ui-setup    Install optional Studio dependencies with npm ci
  .\scripts\dev-windows.ps1 ui-test      Run Studio tests (requires Node.js and npm)
  .\scripts\dev-windows.ps1 smoke        Run demo A against the real provider
  .\scripts\dev-windows.ps1 <command>    Pass a Livium CLI command, e.g. run, status, serve

Set LIVIUM_ENV_FILE to use another .env file. Use a private Windows user directory for
credentials, and keep the file out of shared folders and source control.
"@
        break
    }
    "setup" {
        $PythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $PythonCommand) { throw "Python 3.11 or newer is required on PATH." }
        $VersionText = & $PythonCommand.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
        if ($LASTEXITCODE -ne 0 -or [version]$VersionText -lt [version]"3.11") {
            throw "Livium requires Python 3.11 or newer."
        }
        New-EnvFileIfMissing
        & $PythonCommand.Source -m venv (Join-Path $Root ".venv")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Write-Host "Ready. Edit $EnvFile, then run: .\scripts\dev-windows.ps1 doctor"
        break
    }
    "test" {
        Require-PythonEnvironment
        & $Python -m unittest discover -s tests -t tests
        exit $LASTEXITCODE
    }
    "ui-setup" {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            throw "npm is required for the optional Studio dependencies."
        }
        Push-Location (Join-Path $Root "ui")
        try { npm ci; exit $LASTEXITCODE } finally { Pop-Location }
    }
    "ui-test" {
        if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
            throw "npm is required for the optional Studio tests."
        }
        Push-Location (Join-Path $Root "ui")
        try { npm test; exit $LASTEXITCODE } finally { Pop-Location }
    }
    default {
        Require-PythonEnvironment
        Import-EnvFile
        if ($Action -eq "smoke") {
            & $Python (Join-Path $Root "demos\run_demonstrations.py") --only A `
                --home (Join-Path $Root ".livium\live-smoke")
        } else {
            & $Python -m livium $Action @RemainingArgs
        }
        exit $LASTEXITCODE
    }
}
