# Develop and test Livium in Kali on WSL

This setup keeps Python state in a project virtual environment, stores provider
credentials in an ignored local env file, and gives the first real-provider check its
own disposable Livium home. Livium's Python core uses only the standard library.

## 1. Install or open Kali WSL

From an elevated Windows PowerShell, install Kali if it is not already present:

```powershell
wsl --install --distribution kali-linux
```

Restart Windows if WSL asks, launch Kali, and create its Linux user. Kali documents
`kali-linux` as the WSL distribution name and recommends WSL 2. See the [Kali WSL
guide](https://www.kali.org/docs/wsl/wsl-preparations/) and [Microsoft's WSL install
guide](https://learn.microsoft.com/en-us/windows/wsl/install).

## 2. Install the small Python toolchain

In the Kali terminal:

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
cd /mnt/c/Users/anish/Downloads/Livium/Livium
bash scripts/dev-wsl.sh setup
```

The project is currently on the Windows drive, so `/mnt/c/...` opens the same files
that Codex edits. WSL's Linux filesystem is faster for large builds; if you later move
the checkout under `~/src`, update the `cd` path and keep using that single checkout.

## 3. Put your API key in a private local file

By default, `setup` creates `.env` from `.env.example`. `.env` is ignored by Git. Edit
these fields in `.env`:

```dotenv
LIVIUM_PROVIDER=openai_compatible
LIVIUM_BASE_URL=https://integrate.api.nvidia.com/v1
LIVIUM_API_KEY="paste-your-key-here"
TAVILY_API_KEY="paste-your-Tavily-key-here"
LIVIUM_NETWORK_ALLOWLIST=api.tavily.com
LIVIUM_MODEL=nvidia/nemotron-3-ultra-550b-a55b
LIVIUM_CONTEXT_WINDOW=1000000
LIVIUM_MAX_OUTPUT_TOKENS=4096
```

Use the provider and model that match your key. For Groq, set
`LIVIUM_PROVIDER=groq` and `GROQ_API_KEY=...` (or use `LIVIUM_API_KEY`); its default
base URL is `https://api.groq.com/openai/v1`. `openai_compatible` supports other
OpenAI-compatible endpoints through `LIVIUM_BASE_URL`; Anthropic and Ollama have their
own provider settings. Do not paste the key into a task prompt, terminal command, issue,
or chat. The helper loads `.env` into its own process and does not print values.

The NVIDIA endpoint is paced at 1.6 seconds between request starts (at most 37.5 RPM),
with reservations shared by Livium processes using the same `LIVIUM_HOME`. This leaves
margin under the 40 RPM limit you supplied. It cannot coordinate with other programs,
other Livium homes, or another machine using the same key.

For a credential file that stays entirely inside Kali rather than on `/mnt/c`, use:

```bash
mkdir -p ~/.config/livium
cp .env.example ~/.config/livium/livium.env
chmod 600 ~/.config/livium/livium.env
nano ~/.config/livium/livium.env
LIVIUM_ENV_FILE="$HOME/.config/livium/livium.env" bash scripts/dev-wsl.sh doctor
```

For repeated commands, set the path once in the terminal with
`export LIVIUM_ENV_FILE="$HOME/.config/livium/livium.env"`, or add that export to your
shell profile. The helper never stores it in the project.

## 4. Probe, test, and run a real task

```bash
bash scripts/dev-wsl.sh doctor
bash scripts/dev-wsl.sh test
bash scripts/dev-wsl.sh smoke
bash scripts/dev-wsl.sh run "write a short summary of README.md to summary.md" \
  --criteria '[{"kind":"file_exists","path":"summary.md"}]'
```

`doctor` makes a real provider probe. `test` runs the offline Python suite without
using your provider key. `smoke` runs demonstration A in an isolated
`.livium/live-smoke` home: A1 uses the configured real provider and acceptance check;
A2 is a separately labelled scripted test-double run. The smoke run can use provider
credits. Its output reports a blocked provider as blocked; that is not a successful
model test.

Livium's run-time guardian needs a human-recorded baseline in each new `LIVIUM_HOME`:

```bash
bash scripts/dev-wsl.sh guardian baseline --approved-by "Your Name"
```

The baseline is for the current source and is stored under `.livium/`. Review the
reported paths before accepting it. The human name is an attribution string, not
cryptographic identity verification.

## 5. Optional Studio checks

The browser Studio requires Node.js 22.12 or newer and npm. The Python agent does not
need Node or npm:

```bash
bash scripts/dev-wsl.sh ui-setup
bash scripts/dev-wsl.sh ui-test
```

## Evidence boundary

Passing the offline suite establishes the behaviors its assertions exercise. The real
smoke run establishes that the selected provider can probe, return a completion, use a
tool, and satisfy this smoke criterion on this machine. Neither proves broad model
quality, safe behavior for arbitrary tasks, or complete security isolation. Use
acceptance criteria for every real task and inspect its journal and artifacts.
