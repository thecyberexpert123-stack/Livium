# Changelog

All notable changes to Agent Livium. Every entry states what was verified and how,
because a change that was never exercised is not a change that works.

## Unreleased — a chat-first Studio surface

- Redesigned the existing React/Vite Studio as a chat-first AI workspace: searchable
  task history, provider-status shortcut, responsive welcome and prompt cards, a
  persistent composer, copyable safe Markdown/code, a file-attachment workflow and
  an initially closed workspace drawer. Capability review, Journal, Settings and the
  named-human-only promotion boundary remain available as operator sheets.
- A paperclip or file drop writes the bytes through the actual Studio upload API;
  the resulting server-accepted workspace path is added to the objective. Failed
  uploads or failed task starts show errors and do not silently discard the draft.
  Unsafe Markdown URLs cannot execute, raw HTML is not interpreted, and remote
  images are not fetched by the rendered transcript.
- Tool actions are folded but inspectable; permission refusals, agent-claimed
  summaries and independent acceptance-check verdicts remain distinct. A pending
  run is labelled as waiting for journal events rather than pretending to stream
  model tokens. An unreachable provider stays unavailable; the scripted double is
  explicitly labelled. Each normal send starts a fresh task/model conversation;
  interrupted-task resume is not represented as continuous chat context.
- **Verification:** 368 Python tests, 54 Node/API tests, 14 React component tests;
  `npm audit --audit-level=low` reports no vulnerabilities. Tests cover Markdown
  safety, attachment POST and accepted paths, draft preservation, status, history
  filtering, drawer behaviour, tool inspection and unverified claims. The running
  Studio preview uses the checked-in bundle and its loopback Python child. A hosted
  model still could not be reached from this sandbox; UI fixtures and scripted
  runs are not evidence of live model inference.

## [0.5.0] — a component Studio and a real extension path

### Studio (Node + React/Vite)

- Replaced the hand-wired `ui/public/app.js` page with React components and Vite
  (`ui/src/`). The Node server still owns same-origin APIs and starts real Python
  processes; the Python runtime remains the only agent. Production serves the
  checked-in `ui/public/` bundle with **one** browser-facing Node server, no Vite
  process or npm installation required. Vite's dev server is optional and proxies
  API requests to Node for hot reload.
- The chat, task list, verification, workspace (upload, browse, download, text and
  raster preview), Journal and Settings remain live. A Capabilities sheet shows
  each row, the exact tested/source/installed file hashes, failing or passing
  test output, the Guardian's current gate, and lab source/tests in the dock.
  Promotion stays CLI-only: this console has no authentication.
- **Security fix:** the home browser previously allowed downloading `settings.json`
  *raw*, bypassing response scrubbing and leaking the API key. Preview, download,
  overwrite, deletion and recursive removal through that route now refuse the key
  file, even via a same-inode hard link or a symlinked home; the suite asserts
  these cases over a real socket. Additional environment variables are write-only
  from the browser: their names, not their values, are returned. Long values are
  scrubbed from text even when the variable has an ordinary name. Proxied Python
  JSON is redacted before its outgoing Content-Length is calculated, including
  values escaped on the wire; a real-socket regression covers both. This does not
  protect the files from arbitrary Python with the same OS identity (see §21 of
  `docs/LIMITATIONS.md`).
- `ui/public/source-hash.txt` binds the committed bundle to the React sources;
  tests also fetch the actual hashed assets the Node server serves. React component
  tests drive the settings form, chat, criteria, task submission, artifact dock,
  theme and capability review (UI fixtures, not model evidence).

### Capability growth (Python core, schema v5)

- Agent tools now **actually** scaffold, test and inspect capabilities, following
  `request_capability`. The agent can write a real implementation, helper modules,
  resources and tests in the lab. A passing unittest run plus a conforming tool
  interface records SHA-256 for every lab file and reaches **Validated**; a
  scaffold's placeholder still cannot pass. A read-only `capability review NAME`
  and `/api/capabilities/NAME/review` show tested vs live bytes without importing
  unapproved code.
- Human promotion requires a named operator, a passing Guardian gate and unchanged
  tested files. It atomically copies the **whole package**, including helpers and
  resources, under `$LIVIUM_HOME/promoted/NAME/`; a new agent checks DB status and
  all approved digests **before** import. Unregistered, modified, missing or rolled
  back code fails closed with a recorded load failure. The first successful use
  moves Active to Maintained. A named human can deprecate a superseded tool, or
  rollback a defective one; neither loads on the next run. Blank/whitespace names
  cannot pass the human-attribution check.
- Regression tests exercise proposal → scaffold → real source/test writes via
  the agent's tool calls → validation → human CLI promotion → a new task calling
  the new tool → deprecation → rollback, plus mutation of implementation/tests/helpers, unsigned
  files, package-relative imports and resource files. The model replies are
  explicitly the scripted test double; this does **not** prove model inventiveness,
  test quality or filesystem isolation.
- The default promoted directory moved from the repository to the runtime home.
  Legacy flat modules without recorded manifests are no longer loaded automatically:
  an explicit human review, re-test and re-promotion is required. See
  `docs/LIMITATIONS.md` §9 and §21 for migration and the trust boundary.

**Verification:** 368 Python tests; 53 Node/API tests and 5 React component tests;
all six core demonstrations and M1–M10 rerun with observed outcomes. A1 now uses
only the configured real client for both probe and task, and a regression asserts
an unreachable provider leaves the real task blocked. A previous demo incorrectly
ran the A1 task with the scripted client after a failed real probe; that evidence
was replaced. A2 and the Studio extension test use a labelled scripted test double;
provider egress here still fails, so no model-driven capability generation is claimed.

## [0.4.0] — the Studio

An operator console in Node (`ui/`), with no npm dependencies and no build step. Nothing
in the runtime's behaviour changed, and the Python suite grew by one test for a defect the
console exposed.

### Added

**The console** (`ui/`, new: `server.mjs`, `lib/{paths,settings,backend,session}.mjs`,
`public/{index.html,styles.css,app.js}`)
- Five views over the real runtime: the task history and transcript, a live view of a run
  in progress, the workspace the agent works in, the runtime's own journal, and settings.
- It starts `livium serve` on a free port and proxies `/api/*` to it, and it starts
  `livium run` as a child process per task. Permissions, verification, approvals and the
  ledger stay in the Python runtime: the Studio is a client, not a second agent.
- The API key is entered in the page, stored 0600 in a 0700 directory, and passed to
  children as `LIVIUM_API_KEY` through the environment. Responses carry only
  `api_key_set` / `api_key_length` / a hint; the key is scrubbed from outgoing text and is
  never logged. A test asserts a saved key does not appear in the state payload.
- Workspace containment is checked twice — textually and after following symlinks — over
  HTTP, and both refusals are tested (a link inside the workspace pointing at
  `/etc/passwd` is the case that a single check misses).
- The transcript is derived from journalled events: artifacts carry the digest the
  runtime recorded, a denied tool call is shown as a refusal by the permission engine,
  and a task with no acceptance criteria is labelled *unverified* rather than verified.
  `provider` usage that the provider did not report is shown as an estimate with its
  source, never as `0`.

**Runtime: a probe cache on the read-only status route** (`livium/server.py`)
- `/api/status` probed the provider — a real network request and a real
  `provider_probe` journal row — on *every* call. The viewer polls that route, so a page
  left open filled the journal with its own refresh timer. The answer is now cached for
  five minutes, returned with `age_seconds` and `fresh`, and `?probe=1` asks for a new
  one. `web/app.js` asks once per page load and shows the age next to the badge.
- Tested: three polls of `/api/status` add no `provider_probe` rows; an explicit probe
  adds exactly one; a poll afterwards reports the age and `fresh: false`.

### Fixed

- `ui/server.mjs` served `/static/app.js` and `/static/styles.css` as 404s: the page
  referred to its assets under `/static/`, which is URL vocabulary rather than a real
  directory. The prefix is stripped before the containment check, and a test fetches both.
- `livium/execution.py`: a per-call `timeout` was accepted and ignored, so a command
  that should have been killed after two seconds ran for thirty-two. The limit is now
  clamped to `max(0.5, min(call, configured))` and applied to the kill.
- `livium/execution.py`: when the working directory did not exist, the command died with
  a bare `[Errno 2]` from the spawn — an error that named a file the caller never asked
  for. The directory is created, or the failure names the directory.

### Changed

- `livium/models.py`: the scripted test double can be driven from a file
  (`LIVIUM_SCRIPT_FILE`), so a *separate process* — a demo, the Studio, a recovery test —
  can exercise the loop with a real child process and no network. It is gated behind
  `LIVIUM_ALLOW_TEST_DOUBLE=1` exactly as before, and every reply stays marked as a test
  double. `.env.example` documents both.
- The Studio binds `127.0.0.1` by default and warns when told to bind wider; it has no
  authentication and can write into the workspace.
- The console's layout is a chat shell: tasks on the left with the runtime's verdict on
  each, the conversation in the middle, and the workspace on the right with a preview
  pane that a file card in the transcript opens. The run's process, steps, output and
  artifacts fold into a details block above the messages, and the journal and settings
  moved into sheets over the conversation. Light and dark themes, both from one
  stylesheet.
- `ui/test/ui.test.mjs` (5 tests) checks the page against its script: every id `app.js`
  reaches for exists in `index.html`, no inline handlers, and the stylesheet covers the
  shell it lays out. That is the class of defect that shipped a console which loaded as
  HTML with no behaviour.
- `ui/test/server.test.mjs`: the contract list now names the routes the page *actually*
  calls, and a new test runs a task through the console's own API and asserts the
  transcript it renders — messages, the artifact with its digest, the criteria outcomes
  and the run status — comes back from the runtime that ran it.

## [0.3.0] — integrity response and deferred discovery

Two failures of 0.2.0 are closed. A detected modification of a protected component was
a log line and a frozen boolean, and an observation the agent made while working on
something else was either acted on immediately -- derailing the task that noticed it --
or lost with the context. Nothing from 0.2.0's behaviour was removed.

### Added

**Integrity policy and an append-only record** (`livium/integrity.py`, new)
- One written policy for twelve components in four tiers (`governance`, `enforcement`,
  `instructions`, `operational`), each with *why* it is protected, what a change is
  *permitted* to do, who may *authorise* it, how the change is *verified*, and what
  *weakening* it would look like. The policy lives next to the mechanism that enforces it.
- `integrity_event`: what was detected, the expected state, the policy applied, the
  decision, the human authorisation, the evidence, the recovery and its outcome, the
  residual uncertainty, the attribution and the revision. Append-only **by SQLite
  trigger** -- an `UPDATE` and a `DELETE` are both refused, because the row a tamper
  attempt would most like to rewrite is the row describing the tamper attempt.
- `integrity_snapshot`: one blob per baselined digest, kept in the database so the
  agent's own tools cannot write it.
- `inspect()` per path: `ok | changed | unbaselined | missing`, recording detections.
- `review_change` / `review_candidate`: structural comparison against the baselined
  content (removed definitions, reduced enforcement call-sites), writing nothing.
- `approve_change`: a named human, every weakening finding acknowledged, the enforcement
  probes (and optionally the whole test suite) passing, *then* a re-baseline of that one
  file plus a snapshot. A refusal is recorded with its reason and moves no baseline.
- `restore`: protected components only, a stored snapshot required, atomic replace, and
  the result verified by hashing what is now on disk. An unnamed approver is refused.
- `verify_enforcement`: eleven behavioural probes run in a **separate interpreter, in
  their own home, against a copy of the tree** with any candidate applied. A
  deliberately weakened candidate fails them (`tests/test_integrity.py` asserts the
  failure, so the probes cannot pass by construction).

**Deferred discovery** (`livium/discovery.py`, new)
- A durable queue: provenance (`agent_observation` vs `observed_content`), confidence,
  uncertainty, effort, risk, permissions required, protected files touched, related
  records, spawn lineage, history, outcome and verification evidence.
- A checked transition table (`pending → ready → investigating → completed` and the
  alternatives), a fingerprint that only merges *exact* duplicates, and untrusted-content
  flags for text that reads as an instruction.
- `record_discovery` and `discovery_update` tools, and a router rule
  (`evaluate_discoveries`) that fires **only after the harness has verified the task's
  acceptance criteria** -- the observation is named at the end of the task, never acted
  on inside it.
- `FollowUpCycle`: a budgeted, resumable cycle that investigates an item as *its own
  real task* -- same loop, same tools, same permissions, its own task id and its own
  verification. A crashed investigation is recorded as postponed, never as an outcome.
- CLI: `livium discovery list|show|review|update|investigate` and
  `livium integrity status|events|incidents|review|verify|approve|reject|restore`.
- Viewer panels and two read-only API routes: `/api/discovery` (counts, records, the
  ranking with its reasons, cycle spend and stop reasons) and `/api/integrity` (watched
  components with tier and rationale, the inspection, the append-only record, the
  enforcement history). The poll records nothing: an append-only security table filled
  with a refresh timer is a table nobody reads.

**One enforced follow-up budget** (`livium/discovery.py`, `livium/agent.py`)
- `FollowUpBudget` owns the counters, and `spend()` is the callback the runner passes down
  to its own model calls, its steps, its command executions and its token usage. The charge
  happens **before** the work; a refusal raises and the item returns to `ready` for a later
  cycle. An ordinary task gets a no-op and is charged to nothing, so the only thing that can
  refuse a model call is a budget a cycle deliberately supplied.
- The time limit is a deadline on the same budget rather than a check between items, so an
  item that overruns is stopped while it is running. The token limit is charged from the
  usage the provider reported; because a reply's size is not known in advance, it stops the
  *next* call, and the record says so.
- `task_stopped`, `discovery_investigation_stopped` and `discovery_cycle_stopped` name the
  limit that stopped the work, in one spelling, and `discovery_cycle_complete` carries the
  budget's own report (limits, spend, whether tokens were measured, what was exhausted).
- The learning ledger distinguishes an investigation that concluded from one a budget
  stopped: `followups_investigated` counts conclusions, `followups_stopped` counts the
  attempts left ready (`SCHEMA_VERSION = 4`).

### Changed

- `GrowthGuardian` keeps its public API and delegates policy, snapshots, review and the
  append-only record to `livium.integrity`, so there is one implementation of each.
- The write-refused surface widened from six components to twelve (adding `integrity.py`,
  `approvals.py`, `tools.py`, `verification.py`), and `LIVIUM_EXTRA_PROTECTED_PATHS` lets
  a deployment protect its own files. A file protected only by configuration is still
  watched and still refused; what it lacks is a written rationale, and the record says so
  instead of borrowing one.
- Acceptance verification now runs at the moment the model claims completion (cached per
  task) so the result can decide the next step; the same real checks still run at the end
  of every task, and the model's claim is still never the thing that closes it.
- The learning ledger records `discoveries_captured` and `followups_investigated`,
  counted from the queue rather than from the model's account of what it did.
- `LIVIUM_FOLLOWUP_*` settings bound one follow-up cycle (items, model calls, steps,
  seconds, tokens, chain depth); stopping leaves the rest of the queue ready and journals
  why it stopped.

### Fixed — each found by running the system, not by reading it

- **Unrelated observations were merged into one record.** The duplicate fingerprint
  hashed the *set of long words*, so "one / observation behind one" and "two /
  observation behind two" had the same fingerprint and the second observation was
  silently discarded. The fingerprint is now taken over the whole normalised text;
  similar-but-not-identical text goes through the token-overlap path, which now requires
  three shared significant words before it proposes a merge.
- **The enforcement probes tampered with the live checkout** (`livium/guardian.py`) and
  depended on the operator having baselined their home, so on an unbootstrapped home they
  reported a broken check. They now run in their own home and tamper with a file they own.
- **The follow-up investigation ran commands through the raw subprocess runner**, which
  bypasses the permission engine, the journal and the post-tool integrity check. It now
  goes through the tool registry, so an executed command is checked like any other.
- **`Runner.run` accepted a per-call timeout and ignored it** (a 30-second command ran to
  completion under a 2-second limit). The limit is now applied and clamped to the ceiling.
- **`run_command` reported ENOENT when the workspace directory did not exist** instead of
  creating it, which made a missing directory look like a missing program.
- **The permission policy refused shell metacharacters in arguments** even though no shell
  is ever spawned, blocking legitimate commands while doing nothing about the real risk.
  Metacharacters are now literal text and shell interpreters are refused outright.
- **`record_discovery` passed a `depth` its constructor has no use for**; the depth is
  derived from the spawning record, which is what makes the chain bounded.
- **`register_external` never recorded a capability's provenance**, so every promoted
  capability appeared in the tool catalogue as a builtin.
- **The follow-up budget was checked between items and reported afterwards**, so one
  investigation could spend the whole cycle and the totals were assembled from three
  separate tallies. The counters now live on the budget and are charged by the work itself:
  a follow-up cannot spend more than the budget says.
- **An investigation the budget stopped was counted as evidence of work done.** The ledger
  counted any record whose history mentioned `investigating`, so a stopped attempt was
  reported as investigated while its own record said `ready`. The two are now separate
  columns, and the report prints both.

### Verified

- `python3 -m unittest discover -s tests -t tests` — **358 tests, OK** (24 modules).
- `livium integrity verify` — 11/11 probes pass in 0.13 s; a candidate that stops the
  policy module watching its components fails 2 of them and is recorded as
  `enforcement_verification_failed`.
- `python3 demos/run_mission_demos.py --approve-as "Demo Reviewer"` — **M1–M10 all
  verified**, including the scenarios this release adds (M7 deferred discovery, M8 the queue
  through the CLI, M9 modification → record → review → refusal → verified recovery, M10 an
  investigation stopped by its budget: exactly 2 model calls of 2 allowed, not reported as
  concluded, record left `ready`, ledger 0 concluded / 1 stopped).

### Not verified here

- Live inference against a hosted provider: no reachable endpoint in this environment.
  The clients, wire formats and failure paths are tested; a real completion is not.
- That the enforcement probes catch *every* weakening. They catch the behaviours they
  assert, and the ones they assert are the ones the design claims.

## [0.2.0] — task-directed information flow

An implementation pass over the second specification: store broadly, retrieve
selectively, act deliberately, verify objectively, learn from evidence. Nothing in
0.1.0's behaviour was removed; the loop gained a router, the context gained a
catalogue, and each task now leaves one measured row.

### Added

**Routing and control** (`livium/router.py`, new)
- A deterministic router decides each step among: execute, select an existing
  capability, investigate a failure, retry a recoverable operation, change approach,
  retrieve more context, inspect an unconfirmed side effect, investigate a genuine
  gap, request human authorization, verify completion, persist final state. The order
  of the rules is the safety property: a frozen guardian stops everything, an
  unexplained side effect is established before a task can be closed, and only then is
  the model's completion claim considered.
- Retries are bounded per tool and only for classes where a second attempt can
  plausibly differ (`transient`, `environment`); an identical call is never repeated.
  Limits come from config (`LIVIUM_MAX_RETRIES_PER_TOOL`).
- Tool interfaces are selected per step from the objective and the run's state
  (`select_tools`), so a request carries a handful of interfaces instead of all of
  them. Observed in the demonstrations: 5–7 of 13 exposed per step.

**Learning ledger** (`livium/ledger.py`, new)
- One row per task: category, selected capabilities, injected memory ids, model calls
  and failures, actual prompt/completion tokens when the provider reports them,
  clearly-separated estimates, tools executed, retries, failures, approvals requested,
  verification state, whether an existing capability was reused, whether new
  development was needed, and whether recorded experience materially changed the
  approach — with the evidence for that last claim stored beside it.
- Missing measurements stay `NULL`. They are never written as zero and never replaced
  by an estimate.

**Human approvals** (`livium/approvals.py`, new; `livium approvals` in the CLI)
- Operations the permission engine holds (`LIVIUM_APPROVAL_WRITE_ROOTS`,
  `LIVIUM_APPROVAL_BINARIES`) return `approval_required`, are recorded as a boundary
  event, and stop the run as `awaiting_approval`.
- A decision is CLI-only, must name the human, and covers exactly one execution: the
  approved row is consumed by the call it authorised. A rejection with identical
  arguments is not re-asked.
- There is no HTTP route and no tool through which the model could approve anything;
  a test asserts both.

**Registry and context**
- `capability` rows record description, tools, dependencies, required permissions,
  implementation reference, limitations and version, read from the module that
  actually exists at promotion time; older rows are refreshed from the same module
  (`sync_metadata`), and the loader reports a capability tool as
  `source: capability:<name>` rather than as a built-in.
- `capabilities.search()` answers "what already exists for this requirement?" by
  keyword overlap over name, requirement, description, limitations and provided
  tools; unvalidated candidates are returned only when asked for and always labelled.
- The context manager carries a compact tool catalogue plus the interfaces this step
  selected; `tool_info` fetches any other interface on demand. Full schemas are no
  longer sent for tools the step cannot use.
- `LIVIUM_PROMOTED_DIR` makes the promotion target configurable (an installed package
  may be read-only; a demo must not write into the package it demonstrates).
- `GET /api/ledger` and a viewer panel show the last tasks' measured cost: category,
  status, verification, model calls, tools, retries, failures, actual tokens (or
  `not reported`) and the separate estimate. The endpoint is read-only, and the
  front-end contract test covers it like every other field the page reads.

### Verified

- `livium selftest --record` → 289 tests, exit 0. Nineteen test modules, including
  `tests/test_mission.py` (acceptance criteria A–H), `tests/test_router.py`,
  `tests/test_ledger.py`, `tests/test_approval.py` and `tests/test_hygiene.py`.
- `demos/run_mission_demos.py` — six demonstrations, all recorded `verified` with
  their evidence in `docs/evidence/mission-demos.json`: selective retrieval (37
  records in the store, 2 reaching the prompt), discovery and execution of a promoted
  capability, a refusal that changes the next action and is released exactly once by a
  named human, resume after a process was killed mid-write, cross-process reuse with
  the ledger link, and token accounting (reported usage recorded as actual; absent
  usage left `NULL` with the estimate stored separately).
- `docs/FINAL-REPORT.md` and `docs/evidence/final-report.json` regenerated from live
  state, now 13 points; `docs/evidence/demonstrations.json` regenerated by the
  original six demonstrations.

### Fixed

- **Token usage could never be attributed.** `model_reply` journal events were written
  without a task id, so the ledger read nothing. Every reply event now carries its task.
- **Capability tools were reported as built-ins.** `register_external` dropped the
  provenance it was given and the catalogue defaulted to `builtin` +
  `builtin+tested`. Provenance is stored and asserted by a test.
- **Recovery could not confirm an interrupted write.** The write-ahead intent recorded
  a byte count but the recorded expectation claimed a hash. The intent now records the
  digest it intends to produce and recovery compares it against what is on disk.
- **A per-call command timeout was accepted and ignored** (`Runner.run`), so a caller
  asking for 2 seconds waited for the global limit.
- **A test module had silently lost a test.** `tests/test_mission.py` briefly contained
  duplicated classes and one clobbered body; `unittest` collected 16 of 17 tests and
  reported OK. `tests/test_hygiene.py` now fails when discovery collects fewer tests
  than the sources define.
- **`event_exists` was not a criterion kind.** The verifier's kind is `event`; the
  ledger's classifier and several tests used the wrong name, which turned three
  verifications into `error` results that nothing asserted on.
- `_extract_list` and friends: the stale `permission_decision` counter is gone from
  the report path (decisions are journal events).

### Changed

- `guardian record_baseline` replaces an existing baseline only with `--force`, and
  journals the digests it replaced. Re-baselining is the one action that turns a
  detected change into a trusted one, so it no longer happens as a side effect of a
  setup step.
- A command that is not on the binary allowlist is held for approval rather than
  refused outright: `run_command` will not run it, the request is recorded, and a human
  decides. `LIVIUM_APPROVAL_BINARIES` names programs that are approvable in advance.
- Test fixtures that reuse a home pass `force=True` explicitly; a plain re-baseline in
  a test now fails loudly instead of silently overwriting the previous digests.

### Security

- Approval decisions are unattributable-by-construction: `--by <name>` is required and
  `"agent"`/`"self"`/`"auto"` are refused as approver names.
- The approval queue is keyed on (task, tool, canonical arguments): an approval for one
  task cannot authorise another, and a second execution needs a second decision.
- Protected-surface behaviour is unchanged. Editing `livium/permissions.py` or
  `livium/config.py` shows up as `guardian status → tampered` until a human runs
  `livium guardian baseline --approved-by "<name>"`; this release was developed under
  exactly that condition, and the report says so rather than hiding it.

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
