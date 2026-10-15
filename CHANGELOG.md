# Changelog

All notable changes to Agent Livium. Every entry states what was verified and how,
because a change that was never exercised is not a change that works.

## [0.1.0] — first working build

### Added

**Agent runtime**
- `agent.py`: the operational loop (Receive → Understand → Retrieve → Plan → Act →
  Observe → Verify → Remember → Continue) with stopping conditions enforced by the
  harness: step, tool-call and wall-clock budgets, provider failure, repeated identical
  tool calls, guardian freeze, and completion. A model claiming success does not close
  a task — acceptance criteria do.
- `tools.py`: 12 tools — `read_file`, `list_dir`, `sha256`, `write_file`,
  `run_command`, `memory_search`, `memory_write`, `memory_update`, `memory_forget`,
  `request_capability`, `finish_task`, plus promoted capabilities at load time. Writes
  are atomic (temp file + `os.replace`) and verified by a SHA-256 read-back.
- `verification.py`: executable acceptance criteria (`file_exists`, `file_contains`,
  `run_command`, `memory_exists`, `event_exists`, `manual`). Unmet criteria produce a
  partial result naming what remains; a manual criterion is never counted as verified.

**Persistence and continuity**
- `store.py`: SQLite with WAL, an append-only journal, tasks and steps, a write-ahead
  intent log, context reports, verification rows, capability lifecycle rows, guardian
  baselines, and schema versioning.
- `memory.py`: records with kind, scope, key, confidence, verification state, evidence,
  source and timestamps; FTS5 retrieval with BM25 ranking; trust weighting; duplicate
  detection and consolidation; supersede history; retire-instead-of-delete.
- `recovery.py`: classifies every pending intent from the real filesystem as
  `confirmed_complete`, `not_executed`, `conflicting` or `unknown`, resolves the ones it
  can prove, and leaves the rest for a human rather than guessing.

**Context**
- `context.py`: priority-ordered assembly with mandatory instruction sections first,
  real budgeting against the model's window (explicit override → provider figure →
  built-in table → conservative fallback) minus an output reserve, whole-item trimming,
  and a recorded omission list. A context that cannot hold the mandatory sections
  refuses instead of truncating a rule.

**Growth**
- `capabilities.py`: the lifecycle `proposed → experimental → validated → active →
  maintained → deprecated` with `scaffold`, `test`, `promote --approved-by`, `rollback`
  and `reject`. Promotion installs into `livium/promoted/`, which loads at the next
  startup.
- `guardian.py`: hash baselines for protected files, tamper detection, a frozen mode,
  and a consequence table linking observed behaviour to responses.

**Interfaces**
- `cli.py`: `doctor`, `run`, `resume`, `status`, `events`, `memory …`, `capability …`,
  `guardian …`, `selftest`, `report`, `serve`.
- `server.py` + `web/`: stdlib HTTP API and a dependency-free viewer over the same
  store, so the dashboard cannot display anything the agent does not have.

### Verified

- 205 automated tests, no network and no credentials required, covering the loop,
  tools, permissions, memory, models, context, recovery, verification, guardian,
  capabilities, execution and configuration. Last recorded run: `livium selftest
  --record`.
- Six mandatory demonstrations, executed end to end by
  `demos/run_demonstrations.py`, with results in `docs/evidence/demonstrations.json`:
  A real-task (blocked by environment: no provider egress here; the orchestration
  variant is labelled a test double), B memory across a real process boundary,
  C context inclusion/exclusion and budgeting, D interrupted-write recovery from
  disk evidence, E genuine capability growth (`memory_recent`, promoted by a named
  human and then used by a fresh agent), F a deliberate regression caught by the
  tests with promotion refused and the repair passing.
- Real isolation measured, not assumed: `unshare -rn` removes the network, verified by
  connecting from inside the namespace; the address-space limit is verified by a
  4 GiB allocation failing inside a child; a runaway process is killed at its limit.

### Fixed (found by the tests, fixed in the product)

- A **scaffold could validate itself**: placeholder tests passed and the skeleton
  reached `validated`. `placeholder_markers()` now blocks validation and promotion of
  an unimplemented module, with a `capability_test_refused` journal entry.
- A **per-call timeout was accepted and ignored** by the command runner: a 2-second
  timeout on a 30-second sleep reported success after 32 seconds. The limit is now
  enforced (`max(0.5, min(requested, ceiling))`) and named when the process is killed.
- `int(args.get("limit") or 10)` **silently rewrote an explicit 0** to the default.
  Absent/empty is now distinguished from zero for numeric tool arguments.
- A **command failure pointed at the wrong cause**: a missing working directory
  surfaced as `exit 127` naming the command. The runner now creates it, or reports the
  directory error explicitly.
- The **error path returned an incomplete `ExecResult`** (missing `duration_ms`),
  raising `TypeError` instead of the intended honest 127 result.
- **Unused API surface removed** rather than kept "in case": `tool_schemas_for`,
  `events_since`, `active_names`, `stdout_joined`, `ProbeResult.context_window`, the
  unused `env=` parameter on the command runner (an environment-injection path with no
  caller), and unused imports across the package.
- A **permission rule was broader than the threat**: shell metacharacters in arguments
  were refused even though no shell is ever spawned. Replaced with a rule aimed at the
  real risk (shell interpreters denied; metacharacters stay literal, proven by test).
- The **deny-list had a Windows gap**: `cmd`, `powershell`, `pwsh`, `wsl`, `reg`,
  `diskpart`, `del`, `format` and others were not denied. Added.

### Security posture

- Writes are confined to the workspace and capability lab by the permission engine;
  commands run from an allow-list with a deny-list that cannot be reopened by
  argument; no shell interpreter is permitted; the child environment is built from
  scratch, so credentials in the agent's environment are never inherited.
- Protected paths (`AGENTS.md`, `livium/prompts/*`, `permissions.py`, `config.py`,
  `guardian.py`, `execution.py`) cannot be written by the agent's tools and are hashed
  into the guardian baseline.
- **Not claimed:** filesystem confinement of subprocesses. See
  `docs/LIMITATIONS.md`, which states each boundary by its real strength.
