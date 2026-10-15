# Agent Livium

A small, persistent, experience-driven agent that starts with a minimal foundation
and grows only when a real requirement demands it.

Livium runs a real task loop against a real model provider, executes real tools
under a real permission boundary, keeps persistent memory in SQLite, builds a
*selected* context for each step rather than pasting everything, recovers
interrupted work by inspecting the world instead of replaying it, and develops new
capabilities through a lifecycle where nothing becomes trusted just because it was
written.

**No third-party dependencies.** Python 3.11 standard library only: `sqlite3`,
`http.client`/`urllib`, `subprocess`, `unittest`. Nothing to install, nothing to
audit beyond this repository.

---

## Quick start

```bash
git clone <this repo> && cd Livium

# 1. Tell Livium which model to talk to. Copy the template and edit it.
cp .env.example .env          # then: export $(grep -v '^#' .env | xargs)
export LIVIUM_PROVIDER=openai_compatible
export LIVIUM_API_KEY=sk-...        # or OPENAI_API_KEY
export LIVIUM_MODEL=gpt-4o-mini

# 2. See what actually works in this environment, and what does not.
python3 -m livium doctor

# 3. Establish the guardian baseline once, as a human. Promotion and
#    re-baselining are deliberately not things the agent can do.
python3 -m livium guardian baseline --approved-by "Your Name"

# 4. Run a task. Acceptance criteria are checked for real afterwards.
python3 -m livium run "write notes.md summarising README.md" \
  --criteria '[{"kind":"file_exists","path":"notes.md"},
               {"kind":"file_contains","path":"notes.md","text":"Livium"}]'

# 5. Watch the live state: memory, journal, context reports, capabilities.
python3 -m livium serve --port 8787      # http://127.0.0.1:8787
```

### Or run it with no network and no credential

Livium's agent runtime needs a model to drive it. Everything *around* the model is
independently runnable and testable:

```bash
python3 -m unittest discover -s tests -t tests      # 358 tests, ~35s, no network
python3 demos/run_demonstrations.py                 # the six mandatory demonstrations
python3 demos/run_mission_demos.py --approve-as "Your Name"   # the mission demonstrations
```

---

## Providers

Detection and validation are real: Livium calls the provider, lists its models, and
refuses to run against a model the provider does not offer.

| `LIVIUM_PROVIDER`   | speaks                   | covers                                                     |
| ------------------- | ------------------------ | ---------------------------------------------------------- |
| `openai_compatible` | `/chat/completions`      | OpenAI, OpenRouter, Groq, Together, DeepSeek, vLLM, LM Studio |
| `anthropic`         | `/v1/messages`           | Claude models                                              |
| `ollama`            | `/api/chat`              | any local Ollama model, no credential needed                |
| `scripted`          | nothing                  | **test double only**, requires `LIVIUM_ALLOW_TEST_DOUBLE=1` |

`scripted` is not a provider. Every reply it produces is tagged `is_test_double`,
the CLI prints a warning, and reports distinguish it from real model output.
It exists so the loop, tools, verification, recovery and persistence can be tested
deterministically.

If the provider is unreachable, the task ends `blocked`, with the real error, and
**no completion is invented**. That is the behaviour this repository's own
environment exercises: `api.openai.com`, `api.anthropic.com` and
`generativelanguage.googleapis.com` are not reachable from the sandbox it was built
in, so demonstration A is recorded as blocked-by-environment (see
`docs/LIMITATIONS.md`).

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
| **Capability lifecycle** | `livium capability list/test/promote/rollback`; demonstrations E and F |
| **Guardian integrity** | `livium guardian status`; tamper tests in `tests/test_guardian.py` |
| **Step routing** | `livium events --type route_decision`; `tests/test_router.py` |
| **Selective exposure** | `livium status` context reports list the interfaces sent per step — 5 to 7 of 13 in the recorded mission run |
| **Approval boundary** | `livium approvals --pending` then `--approve <id> --by "Your Name"`; the operation runs once, and only after the decision |
| **Token accounting** | `livium status` ledger rows: `prompt_tokens_actual` is `NULL` unless the provider reported usage; the estimate is a separate column |
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
livium capability list|show|propose|scaffold|test|promote|rollback|reject
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
| `LIVIUM_PROMOTED_DIR` | `livium/promoted` | where promoted capabilities are installed; relative paths resolve under `LIVIUM_HOME` |
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
verification, capability lifecycle, guardian controls, viewer, HTTP API, 358 automated
tests, deterministic step routing, selective tool exposure, the per-task learning
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
