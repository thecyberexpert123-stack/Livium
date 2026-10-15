# Develop and test Livium on native Windows

Native Windows uses the standard-library Python runtime. It does not use Bash,
WSL, or the Linux virtual-environment paths.

## Requirements

- Windows 10 or 11
- Python 3.11 or newer on `PATH`
- PowerShell 5.1 or PowerShell 7
- Node.js and npm only for the optional Studio tests

## Set up

From PowerShell in the project directory:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 setup
notepad .env
```

Set the provider values in `.env` and save it. For Tavily web search, set
`TAVILY_API_KEY` and `LIVIUM_NETWORK_ALLOWLIST=api.tavily.com`. The helper reads
dotenv assignments as data; it never evaluates the file as PowerShell. Keep `.env`
private and out of shared or synchronized folders. `LIVIUM_ENV_FILE` can point to a
private alternative.

## Test and run

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 test
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 doctor
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 smoke
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 serve
```

The offline Python suite does not use the provider key. `doctor` probes the configured
provider. `smoke` and real tasks can make billable API calls. `serve` listens on
`127.0.0.1:8787`; keep its terminal open and visit that address in a browser.

Optional Studio checks require Node.js and npm:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 ui-setup
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 ui-test
```

## Windows execution boundary

Livium continues to enforce its command allow-list, uses direct argument arrays
without a shell, applies timeouts, caps captured output, and excludes provider
credentials from child environments. Windows has no Linux `unshare` network namespace
or POSIX `resource` limits, so the runtime reports those protections as unavailable.
It also does not confine child filesystem access. Do not use this mode to run hostile
generated code; use an appropriately isolated Linux container or VM for that threat
model.
