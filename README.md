# Agent Livium

A small, persistent, experience-driven agent that starts with a minimal foundation
and grows only when a real requirement demands it.

Livium runs a real task loop against a real model provider, executes real tools
under a real permission boundary, keeps persistent memory in SQLite, builds a
*selected* context for each step rather than pasting everything, recovers
interrupted work by inspecting the world instead of replaying it, and develops new
capabilities through a lifecycle where nothing becomes trusted just because it was
written.

**Core:** Python 3.11 standard library only (`sqlite3`, `http.client`/`urllib`,
`subprocess`, `unittest`). **Studio:** React + Vite + a real Node.js server; npm
packages are used to build and test the browser UI. Its committed production bundle
runs with Node alone, without an npm install or a build step on the deployed host.

---

## Quick start

```bash
git clone <this repo> && cd Livium

# 1. Prepare the environment and edit the ignored .env file.
python3 -m venv .venv
cp .env.example .env
nano .env                       # set LIVIUM_API_KEY; keep the value private

# 2. See what actually works in this environment, and what does not.
bash scripts/dev-wsl.sh doctor

# 3. Establish the guardian baseline once, as a human. Promotion and
#    re-baselining are deliberately not things the agent can do.
bash scripts/dev-wsl.sh guardian baseline --approved-by "Your Name"

# 4. Run a task. Acceptance criteria are checked for real afterwards.
bash scripts/dev-wsl.sh run "write notes.md summarising README.md" \
  --criteria '[{"kind":"file_exists","path":"notes.md"},
               {"kind":"file_contains","path":"notes.md","text":"Livium"}]'

# 5. Watch the live state: memory, journal, context reports, capabilities.
bash scripts/dev-wsl.sh serve --port 8787   # http://127.0.0.1:8787
```

For Kali Linux under WSL, including isolated environment setup, private API-key
storage, offline tests, and a real-provider smoke run, see
[`docs/WSL-KALI-SETUP.md`](docs/WSL-KALI-SETUP.md).

### First skill: Tavily web search

Set `TAVILY_API_KEY` in the ignored `.env` file, and set
`LIVIUM_NETWORK_ALLOWLIST=api.tavily.com` to permit this fixed outbound host. The
`web_search` tool returns bounded snippets with source URLs; the API key is never
included in tool results or the task journal. Calls need internet access and may use
your Tavily plan. Both the WSL and Windows helpers load the `.env` values. In Studio,
put both variables in **Settings → Extra environment** and restart the backend; those
values are write-only in the UI.

### Native Windows

Livium also supports native Windows with Python 3.11+ and PowerShell. Use the
PowerShell helper instead of the Bash/WSL helper:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 setup
notepad .env                         # set LIVIUM_API_KEY; keep it private
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 test
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 doctor
powershell -ExecutionPolicy Bypass -File .\scripts\dev-windows.ps1 serve
```

Open `http://127.0.0.1:8787` while `serve` is running. Windows does not provide
Linux namespaces or POSIX resource limits, so the runner reports those controls as
unavailable; do not use native Windows mode to execute hostile code. See
[`docs/WINDOWS-SETUP.md`](docs/WINDOWS-SETUP.md).

### Reproduce without a credential

Livium's agent runtime needs a model to drive it. The Python and UI suites run without
external network access. The six-demonstration script attempts a real provider for
A1; if unreachable, it records a blocked task, and only A2 uses a labelled test double:

```bash
python3 -m unittest discover -s tests -t tests      # 368 tests, no external network
python3 demos/run_demonstrations.py                 # six demonstrations; A1 attempts a real provider
python3 demos/run_mission_demos.py --approve-as "Your Name"   # ten mission demonstrations
cd ui && npm ci && npm test                         # build + 54 Node/API + 14 React tests
```

---

## Providers

Detection and validation are real: Livium calls the provider, lists its models, and
refuses to run against a model the provider does not offer.

| `LIVIUM_PROVIDER`   | speaks                   | covers                                                     |
| ------------------- | ------------------------ | ---------------------------------------------------------- |
| `openai_compatible` | `/chat/completions`      | OpenAI, OpenRouter, Together, DeepSeek, vLLM, LM Studio    |
| `groq`              | `/chat/completions`      | Groq API with its default URL and Groq-specific API key    |
| `anthropic`         | `/v1/messages`           | Claude models                                              |
| `ollama`            | `/api/chat`              | any local Ollama model, no credential needed                |
| `scripted`          | nothing                  | **test double only**, requires `LIVIUM_ALLOW_TEST_DOUBLE=1` |

`scripted` is not a provider. Every reply it produces is tagged `is_test_double`,
the CLI prints a warning, and reports distinguish it from real model output.
It exists so the loop, tools, verification, recovery and persistence can be tested
deterministically.

For Groq, select `LIVIUM_PROVIDER=groq` and provide `GROQ_API_KEY` (or the generic
`LIVIUM_API_KEY`). The default endpoint is `https://api.groq.com/openai/v1`; model IDs
are provider-managed and should be selected from Groq's current model catalog.
When an HTTP 429 response includes a retry delay, Livium waits for that provider hint
and retries within `LIVIUM_MODEL_RETRIES` (up to 60 seconds per wait); each retry is
journalled. If the delay is longer, no hint is supplied, or retries are exhausted, the
task reports the provider rejection instead of looping. Provider token usage is reduced
to validated numeric counts, while malformed or unavailable usage remains unmeasured.

If the provider is unreachable, the task ends `blocked`, with the real error, and
**no completion is invented**. That is the behaviour this repository's own
environment exercises: `api.openai.com`, `api.anthropic.com` and
`generativelanguage.googleapis.com` are not reachable from the sandbox it was built
in, so demonstration A1's real task is recorded as blocked-by-environment while
A2 is separately labelled a scripted test-double run (see `docs/LIMITATIONS.md`).

---

## What is real, and how you can check it

| Capability | Evidence you can reproduce |
| --- | --- |
| **Task loop** | `demos/run_demonstrations.py` → A2; `livium run … --criteria …` |
| **Real tools** | file writes are atomic and read back with a SHA-256 comparison; commands run under a real namespace |
| **Permission boundary** | `tests/test_permissions.py` (symlink escape, `..`, denied binaries, git guards); every refusal is a `boundary_violation` row in the journal |
| **Persistent memory** | `tests/test_memory.py`; `livium memory search "…"`; writer/reader subprocesses in demonstration B |
| **Context management** | `livium status` shows `context_report` rows: what was included, what was omitted, the real budget |
| **Task recovery** | `livium resume`; demonstration D confirms an interrupted write from the file on disk |
| **Verification** | acceptance criteria are executed by the harness; `livium events --type task_verified` |
| **Capability lifecycle** | the agent can propose → scaffold → write source/helpers/tests → run actual tests to Validated; a named human reviews exact digests and promotes, a later process loads the new tool; `tests/test_capabilities.py` and the Studio live API test |
| **Guardian integrity** | `livium guardian status`; tamper tests in `tests/test_guardian.py` |
| **Step routing** | `livium events --type route_decision`; `tests/test_router.py` |
| **Selective exposure** | context reports list the interfaces sent per step; the catalog stays lazy and the step ceiling is 10 |
| **Approval boundary** | `livium approvals --pending` then `--approve <id> --by "Your Name"`; the operation runs once, and only after the decision |
| **Token accounting** | `livium status` ledger rows: `prompt_tokens_actual` is `NULL` unless the provider reported usage; estimates are separate, and `model_request_attempt` journals each transport attempt |
| **Mission acceptance tests** | `python3 -m unittest discover -s tests -t tests -p "test_mission.py"` |
| **Network isolation** | `livium doctor` reports the mechanism it *verified*, not the one it hoped for |

## Commands

```
livium doctor                     runtime, provider probe, isolation, permissions, integrity
livium run "objective" [--criteria JSON|@file] [--resume] [--force]
livium resume [--task-id X]       reconstruct an interrupted task; says whether resuming is safe
livium status                     configuration, stats, capabilities, recent context reports
livium events [--type T]          read the append-only journal
livium memory search "query"      ranked full-text retrieval with provenance and trust
livium memory list|history|duplicates|consolidate|stale
livium capability list|show|propose|scaffold|test|review|promote|deprecate|rollback|reject
livium approvals [--pending] [--history] | --approve ID --by NAME | --reject ID --by NAME
livium guardian status|baseline --approved-by NAME|propose-change …
livium discovery list|show|review|update ID STATUS --note WHY|investigate ID
livium integrity status|events|incidents|review [PATH]|verify [--tests]
livium integrity approve PATH --approved-by NAME --reason WHY [--acknowledge FINDING]
livium integrity reject  PATH --by NAME --reason WHY
livium integrity restore PATH --approved-by NAME --reason WHY
livium selftest [--record]        run the repository test suite; --record feeds `livium report`
livium serve [--port 8787]        HTTP API + live viewer
```

---

## Studio

`ui/` is a **React + Vite** component application served by a **Node.js**
operator server. A built production bundle is committed in `ui/public/`; you need
Node >=22.12 to run it, but not npm on the deployed machine:

```bash
node ui/server.mjs --port 8899          # http://127.0.0.1:8899, one UI process
cd ui && npm ci && npm test             # rebuild, test real server + React components
# Development only, optional hot reload: in one terminal run node ui/server.mjs;
# in another: cd ui && npm run dev. Vite proxies same-origin /api and /studio/api
# to http://127.0.0.1:8877 by default (set LIVIUM_STUDIO_PROXY for another port).
```

Production uses **one browser-facing Node server**, not a parallel Vite server. It
spawns `livium serve` on loopback and proxies its actual data, plus one `livium run`
process per task. Node is not a second permission engine, verifier or model client.
The checked-in bundle has a source digest (`ui/public/source-hash.txt`); tests fail
if a source edit is shipped without rebuilding it.

It opens as a **chat-first AI workspace**: a welcome view, prompt suggestions and a
persistent composer are primary. Recent tasks and a compact connection indicator live
in the searchable sidebar. The file browser is initially closed and opens as a drawer
on demand. Light and dark themes and narrow screens are supported.

| Where | What it is for |
| --- | --- |
| **Sidebar** | search the latest 60 real tasks, see their recorded verdict, start a **New chat**, check whether the configured provider is usable or a labelled test double, and open the operator sheets |
| **Conversation** | journal-backed messages with safe Markdown, copyable replies/code, collapsible recorded tool activity, visible permission refusals and artifacts. The agent's own final summary is labelled as a claim; acceptance checks have a separate runtime verdict. An artifact's **Open** button opens it in the workspace drawer |
| **Composer** | `Enter` sends, `Shift+Enter` adds a line. Optional JSON acceptance criteria, force and interrupted-task resume are under **Acceptance criteria & options**. The paperclip or a file drop uploads *real bytes* to the runtime workspace and puts the server-accepted file path in the task objective; it does not attach an in-memory blob directly to a model request |
| **Workspace drawer** | browse, preview, download, upload, create folders, delete and inspect recent files in the real workspace/lab/promoted roots; hidden by default |
| **Run details** | expandable process state, recorded steps, output and artifact digests beneath the conversation; the UI shows progress as journal rows arrive, not fabricated token streaming |
| **Capabilities** | proposed, experimental, validated, active, maintained and rolled-back capabilities, test output, SHA-256 manifest of *all* lab files, current-vs-tested comparison and the Guardian's live gate. Open lab code in the drawer; human promotion stays CLI-only because the Studio has no login |
| **Journal / Settings** | the runtime's own events, deferred work, integrity and ledger rows; provider, model, base URL and write-only API key. Failed probes remain failures, with an actionable settings link, not a fallback to the scripted double |

**Important chat distinction:** by default each send starts a **new task and fresh
model conversation**, even when the preceding task is still visible on screen. The
**Resume** exception resumes an interrupted *task*, not conversational memory or a
follow-up turn.
The UI does not invent cross-task context or token streaming. Use **New chat** to clear
the current view. A task with no acceptance criteria remains *unverified*. See
`docs/LIMITATIONS.md` §20 for the remaining trust boundaries.

The console has three settings of its own; they are environment variables because they
belong to the console's own process, and `.env` is the runtime's file:

| Variable / flag | Default | Meaning |
| --- | --- | --- |
| `LIVIUM_STUDIO_HOME` / `--home` | `<repo>/.livium` | where `settings.json` lives, and the runtime home the console drives |
| `LIVIUM_PYTHON` | `python3` | the interpreter that has this repository on `PYTHONPATH` |
| `LIVIUM_STUDIO_NO_BACKEND=1` | unset | do not spawn `livium serve` (the runtime is already running elsewhere) |
| `--host 0.0.0.0` | `127.0.0.1` | bind wider; the banner warns, because there is no authentication |

Three things are worth knowing before you use it:

- **The API key is written, not remembered.** It goes to `<studio home>/settings.json`
  (mode 0600, directory 0700) and reaches the runtime through the child process
  environment. The API returns whether a key is set, how long it is and a hint — never
  the key — and outgoing error text is scrubbed. The home file browser refuses
  `settings.json`, even as a raw download. Additional environment values are also
  write-only in the UI: their names are shown but the values are never sent back.
  **This is an HTTP guarantee, not an OS isolation guarantee:** arbitrary Python
  run as the same user can still read that file. Do not paste secrets into a task
  or chat message.
- **It is a console, not a second agent.** Every permission decision, verification and
  journal row is produced by the Python runtime; the Studio starts a real `livium run`
  and shows what the runtime recorded. A panel with no records says so instead of
  rendering something plausible.
- **It has no authentication.** It binds `127.0.0.1` by default; `--host 0.0.0.0` is for
  when you put your own authentication in front of it, and the banner warns when you do.

### Building a reusable capability

The agent has real `request_capability`, `capability_scaffold`, `write_file`,
`capability_test` and `capability_status` tools. It can compose or reuse existing
tools first, then create a named lab package (`<home>/capability-lab/<name>/`),
write arbitrary Python helpers/resources and behaviour tests there, and run those
tests under the runtime's *reported* isolation. Passing tests record a digest for
every authored file and move the capability to **Validated**. A scaffold alone
cannot pass. Validated code is still unavailable as a tool.

The human can inspect the code/tests in the Studio's **Capabilities** sheet and
compare the live lab digest to the tested one; or use the CLI:

```bash
python3 -m livium capability review NAME
# If needed, after inspecting the protected files:
python3 -m livium guardian baseline --approved-by "Your Name"
python3 -m livium capability promote NAME --approved-by "Your Name"
# A NEW task loads the new tool if the installed package still matches the approval.
python3 -m livium capability deprecate NAME --approved-by "Your Name" --reason "superseded"
# Or withdraw a defective tool while preserving the rollback record:
python3 -m livium capability rollback NAME --reason "why it was withdrawn"
```

Promotion copies the **whole package** (not just one `.py`) into
`$LIVIUM_HOME/promoted/NAME/`: helpers, data and tests. A post-validation edit
requires re-testing; an unregistered or altered promoted package is refused
*before import*. The first successful call moves Active to Maintained. The review
step is an attributed **workflow**, not strong human authentication: the Studio
has no login, and `run_command`/promoted Python has the OS user's filesystem
privileges. For adversarial untrusted code, use a separate OS identity or a real
filesystem sandbox; see `docs/LIMITATIONS.md` §21. A real model has not driven
this build loop in this sandbox: the integration tests deliberately use a labelled
scripted test double, never call it model-verified.

---

## Configuration

All configuration is environment-based (`livium/config.py` is the single source of
truth). The important ones:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LIVIUM_HOME` | `./.livium` | database, workspace, capability lab, evidence |
| `LIVIUM_WORKSPACE` | `$HOME/workspace` | the only place the agent may write |
| `LIVIUM_CONTEXT_WINDOW` | model table / 8192 | the model's real window; overrides everything |
| `LIVIUM_ALLOWED_BINARIES` | a small list | what `run_command` may execute |
| `LIVIUM_DENIED_BINARIES` | larger list | shells, `rm`, `curl`, `pip`, `sudo`, … |
| `LIVIUM_NETWORK_ALLOWLIST` | empty | hosts the agent may reach; empty means none |
| `LIVIUM_MAX_STEPS` / `LIVIUM_MAX_TOOL_CALLS` / `LIVIUM_MAX_SECONDS` | 12 / 40 / 900 | loop stopping conditions |
| `LIVIUM_MAX_RETRIES_PER_TOOL` | 2 | how often one recoverable operation may be retried before the router changes approach |
| `LIVIUM_PROMOTED_DIR` | `$LIVIUM_HOME/promoted` | per-home, human-promoted packages with source/test manifests; relative paths resolve under `LIVIUM_HOME` |
| `LIVIUM_APPROVAL_WRITE_ROOTS` | empty | paths the agent may write **only after a human approves** that specific call |
| `LIVIUM_APPROVAL_BINARIES` | empty | programs the agent may run only after a human approves that specific call |
| `LIVIUM_EXTRA_PROTECTED_PATHS` | empty | files this deployment refuses to let the agent write, watched and recorded like the built-in surface |
| `LIVIUM_FOLLOWUP_ENABLED` / `LIVIUM_FOLLOWUP_MAX_ITEMS` / `_MAX_MODEL_CALLS` / `_MAX_STEPS` / `_MAX_SECONDS` / `_MAX_TOKENS` / `_MAX_DEPTH` | 1 / 2 / 6 / 6 / 120 / 20000 / 2 | what one follow-up cycle may spend, and how deep a chain of observations may go |

Run `python3 -m livium doctor --json` for the complete effective configuration.

---

## Documentation

- `ARCHITECTURE.md` — modules, data flow, and the decisions behind them
- `docs/LIMITATIONS.md` — what this is **not**, stated plainly (read this one)
- `AGENT-EXPERIENCE.md` — lessons recorded while building it, including failures
- `CHANGELOG.md` — what changed and what was verified
- `AGENTS.md` — the operating instructions the agent itself follows
- `.env.example` — every supported setting, checked against the code by a test
- `docs/evidence/` — recorded output of the demonstrations and the final report
- `demos/run_demonstrations.py` — re-runs all six demonstrations from a fresh clone
- `demos/run_mission_demos.py` — the mission demonstrations, run for real: selective
  retrieval, capability discovery, a failure that changes the next action, resume after
  interruption, reuse across processes, token accounting (M1–M6, and M7 deferred
  discovery, M8 the queue through the CLI, M9 modification → record → review → refusal →
  verified recovery, M10 a follow-up stopped by its budget)

## Honest status

Implemented and verified: task loop, tools, permissions, memory, context, recovery,
verification, capability lifecycle, guardian controls, viewer, HTTP API, the current
Python/Node/React suites above, deterministic step routing, selective tool exposure, the per-task learning
ledger, human-approved operations that run exactly once per decision, an append-only
integrity record with an enforced review/approval/recovery workflow, and a deferred
discovery queue whose follow-up investigations run as their own tasks on a budget that is
charged before each call, so a stopped investigation is recorded as stopped rather than
concluded.

The viewer and the API show that state rather than describing it: `/api/discovery` returns
the queue and the ranking *with its reasons*, `/api/integrity` returns the watched set, the
inspection, the append-only record and the enforcement history. Both are read-only; the
human-attributed operations stay on the CLI.

The integrity claims are behavioural, not documentary: `livium integrity verify` runs
eleven probes in a separate interpreter against a copy of the tree, and a candidate that
weakens a control fails them. Where the layer cannot prevent something — an agent that
already has OS-level privileges — it detects, attributes, freezes and records instead,
and `docs/LIMITATIONS.md` says so in those words.

Implemented but **not verified here**: live inference against a real provider
(no reachable endpoint in the build environment). The HTTP clients are real,
their wire formats are asserted by tests, and their failure behaviour is verified —
but no completion from a hosted model has been observed.

Partially implemented, with reasons: filesystem confinement for subprocesses
(network isolation is real; filesystem isolation is not claimed), and the
instruction self-improvement loop, which produces reviewable proposals but never
edits the instructions itself.
