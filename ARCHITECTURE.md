# Agent Livium — Architecture

## Design premise

The specification's central requirement is that nothing pretend to be more than it
is. Every design decision below therefore answers one question: *what is the smallest
real mechanism that actually does this job?* Where a mechanism cannot be made real
under the constraints (for example, in-process sandboxing of subprocesses while
keeping stdlib-only), the architecture states the limitation instead of papering over
it — see `docs/LIMITATIONS.md`.

Two consequences shape everything:

1. **Standard library only.** No dependency can silently become the thing that makes
   the system appear to work. The cost is that isolation and reasoning quality are
   closer to the floor; the benefit is that every behaviour is inspectable in this
   repository.
2. **The model is a component, not the system.** The loop, permissions, memory,
   context, verification, recovery, and capability lifecycle all run and are tested
   without any provider. The model contributes judgement at exactly one point: the
   next action.

## Module map

```
livium/
  config.py        configuration, paths, budgets  — the single source of truth
  store.py         SQLite schema, journal, tasks, intents, context reports, migrations
  permissions.py   path/filesystem/command/network/credential boundary + decisions
  execution.py     the only place a subprocess is created: namespace + timeout + kill
  models.py        provider clients (openai_compatible | anthropic | ollama | scripted),
                   capability probing, context-window truth, usage accounting
  memory.py        records, FTS retrieval, ranking, dedupe, supersede, trust, lifecycle
  tools.py         the tool registry: specs, argument validation, dispatch, side effects
  verification.py  executable acceptance criteria + the honest summary of what passed
  guardian.py      integrity baselines, violation rules, promotion gate, frozen mode
  router.py        the deterministic step decision: execute / select capability / retry /
                   investigate / retrieve / verify / persist / request authorization
  ledger.py        one measured row per task: what was used, retried, verified, and
                   whether recorded experience changed the approach
  approvals.py     operations held for a human decision, one execution per decision
  context.py       context assembly, prioritisation, budgets, omission records
  recovery.py      interrupted-work inspection and resume safety
  capabilities.py  proposed → experimental → validated → active → maintained → deprecated
  agent.py         the operational loop; ties the above together and persists state
  cli.py           operator surface
  server.py        HTTP API + viewer (stdlib http.server)
  web/             dependency-free ES-module front end
  prompts/         protected instruction files (FOUNDATIONAL, CONTRACT, GROWTH)
  promoted/        human-promoted capability modules, loaded at startup
```

Dependency direction is one-way: `agent` → everything; `tools` → permissions,
execution, memory, capabilities; `router` → nothing but its own rules; `context` →
memory, capabilities, guardian; `store` → nothing. Nothing imports `agent`.

## The operational loop

`agent.run_task(objective, criteria)`:

```
Receive    objective + acceptance criteria recorded before any model call
Understand build context (objective, instructions, memories, limits, state)
Retrieve   memory chosen by relevance to the objective, not everything
Plan       the model chooses a tool call, or finishes
Act        tool dispatch: permission check → intent recorded → real execution
Observe    real result (exit code, bytes, sha256, rows) appended to state
Verify     acceptance criteria executed by the harness, never by the model
Remember   outcome, lessons, and failures written to memory
Continue   next step, or stop — with a stopping condition from the list below
```

**Stopping conditions**, all enforced in the loop rather than trusted to the model:
step budget (`max_steps`), tool-call budget, wall-clock budget, refusal by the
permission engine, provider failure, repeated identical failure, and the model calling
`finish_task`. Reaching any budget produces an honest terminal state — `blocked`,
`failed`, or `partial` — never a silent success. The model's completion claim does not
close a task: `task_verified` decides, and a task with unmet criteria is reported as
partial with the remaining criteria named.

**Write-ahead intents.** Before a side effect, the store records an intent
(`store.record_intent`). After it, the intent is confirmed. An intent with no
confirmation is precisely what recovery examines.

### The step is decided, not improvised

`router.TaskRouter.route()` takes the observable state of the run (step number,
remaining budgets, the last failures with their classification, retry counts, whether
anything is unresolved, whether the model already asked to finish, whether the
guardian is frozen) and returns exactly one `RouteDecision`. The rule order is the
safety property:

```
frozen → unresolved side effect → finish requested → denied → failed → repeated →
no progress → genuine gap → execute
```

A frozen guardian stops the run before anything else is considered. An unresolved
side effect is established before a task can be closed, whatever the model claims.
Only after those come the ordinary cases. The model recommends an action; the router
decides whether that recommendation can be acted on, and the decision is journalled
(`route_decision`) with the reason and the evidence behind it.

**Selective exposure.** `select_tools()` chooses the interfaces for the step from the
objective, the tools the run has already used, and the registry — never the whole
catalogue. The recorded mission run sent 3–6 of 13 interfaces per step. The compact
catalogue in the context makes every *other* interface discoverable through
`tool_info`, so selection narrows attention without hiding capability.

**Retries are bounded and classified first.** `classify_failure()` separates a
permission refusal, a bad argument, a missing binary, a command failure and an
unknown tool. Only `transient` and `environment` failures are retried
(`LIVIUM_MAX_RETRIES_PER_TOOL`, default 2); everything else changes approach. A
failed action never becomes a capability proposal by itself — a gap has to be
classified as one first, and the reuse ladder runs before the model may treat it as
real.

### The learning ledger

Every task writes one row (`ledger.py`): category, the capabilities selected, the
memory ids that reached the prompt, model calls and failures, tools executed, retries,
denials, approvals requested, failures, verification state, status, duration, and
three questions the specification asks directly — was an existing capability reused,
was new development needed, and did recorded experience change the approach. The last
one is not a feeling: it is computed from the injected records against the tools that
actually ran, and the evidence for it is stored in the row.

Token accounting keeps actual and estimated in different columns. `prompt_tokens_actual`
is `NULL` unless a provider reported usage; the estimate from the context builder lives
in `tokens_estimated`. Nothing is written as zero to look complete.

### Approvals

Some operations are neither allowed nor forbidden: writing under
`LIVIUM_APPROVAL_WRITE_ROOTS` or running a program in `LIVIUM_APPROVAL_BINARIES`
yields `approval_required`, which records a request, journals a boundary event, and
stops the run as `awaiting_approval`. The decision is made only through
`livium approvals --approve <id> --by "<name>"` — there is no HTTP route and no tool
that can decide one, and a test asserts both. A decision covers exactly one execution
of one operation for the task that asked; the row is consumed by that call, and a
second identical attempt needs a second decision. A rejected operation is not
re-asked with the same arguments.

Verification may read inside an approval write root (`check_verification_read`) even
though ordinary reads cannot: without it, an operation a human authorised could be
performed but never checked, and every such task would end `not verified` for a reason
unrelated to the work. The relaxation is one line, confined to paths a human put in the
configuration, and it returns its reason like every other decision.

## Data model (SQLite, WAL)

| Table | Purpose |
| --- | --- |
| `memory` | records: kind, content, key, confidence, verification, evidence, trust, supersession, retired state |
| `memory_fts` | FTS5 index over content, kept in sync by triggers |
| `memory_history` | every prior version of an edited record — instruction changes never lose history |
| `task` | objective, criteria, status, next action, outcome, recovery requirements |
| `task_step` | per-step record: phase, tool, arguments, result, timing |
| `intent` | write-ahead log for side effects |
| `event` | append-only journal (the primary evidence trail) |
| `verification` | each criterion and its executed result |
| `context_report` | what each context build included, omitted, and estimated |
| `capability` | lifecycle state, reuse analysis, verification plan, promotion record |
| `guardian_baseline` | hash, size, mtime, approver, and a note for every protected file |
| `permission_decision` | every allow/deny with the rule that decided it |
| `schema_migrations` | applied versions, so a database is never silently mis-versioned |

Deleting is not how memory works: `memory_forget` retires a record, keeps the row and
its history, and marks it inactive. Retrieval returns active records only.

## The parts worth explaining

### Permissions are a check, not a promise

`PermissionEngine` decides on real inputs and records the deciding rule:
path resolution including symlinks and `..`, an allow-list for binaries with a
deny-list that cannot be re-enabled by configuration, an argument scan that refuses
control characters, and network access by host allow-list (empty by default).
Notably, shell metacharacters are **not** treated as dangerous: no shell is ever
spawned, `subprocess` receives an argv list, so `;` and `&&` reach the program as
literal text. What is refused is `sh`, `bash`, `cmd`, `powershell` and friends —
because those *would* turn text into syntax. The earlier, broader rule was removed
after it blocked legitimate Python one-liners; the reasoning is in the code, and
`tests/test_permissions.py` pins both halves.

The agent's own instructions cannot widen these rules. `AGENTS.md` is untrusted input
like any other file: it is read for relevance, not obeyed.

### Execution: real isolation, honestly labelled

`execution.py` is the only module that creates a process. It uses
`unshare -rn` (verified at runtime, not assumed) plus a working directory, a timeout
with process-group kill, and output caps. That is **process-level** isolation:
network namespace and process namespace are real; filesystem confinement is *not*
claimed, because a writable workspace does not confine a program that can read `/etc`.
`livium doctor` and the viewer report which mechanisms it actually verified, and the
viewer shows `network_isolated: true` next to `filesystem_confined: false` for that
reason. Nothing in this codebase is named `Sandbox`.

### Memory is evidence, not truth

Every record carries `verification` (`claimed`, `tested`, `verified`, `refuted`,
`unclear`), `confidence`, `evidence`, `source`, and timestamps, plus `supersedes` for
corrections and `retired_at` for forgetting. Ranking blends FTS relevance with trust
and recency; `memory_stale` downgrades old `claimed` records rather than deleting
them. Retrieval is deterministic and explainable: a query either matches records or
it does not, and the tool reports which. An unanswerable query returns zero rows —
which is how a genuine capability gap was discovered rather than invented.

The instruction-change pathway is deliberately asymmetric: the agent may write a
*proposal* with rationale and evidence, and a human approves the change. Prior
versions stay in `memory_history`. Nothing auto-edits the instructions after a single
failure.

### Context is a budget, not a concatenation

`ContextManager.build()` assembles a prompt under a real budget derived from the
model's actual limit (measured, or from a built-in table, or the configured
`LIVIUM_CONTEXT_WINDOW`) minus a hold-back for output. Sections are prioritised —
mandatory instructions first, then objective and criteria, then relevant memory, then
execution state — and **every omission is recorded** in `context_report`, so "the
agent forgot" can be investigated instead of guessed at. Memory selection is scored
against the objective; irrelevant records are excluded rather than truncated, and
whole items are dropped before content is cut mid-record (an elision marker keeps it
explicit). If the output reserve and mandatory rules cannot fit, the build refuses and
says so rather than truncating a rule.

Token estimates are labelled estimates: the `chars/4` heuristic is calibrated from
real token usage when the provider reports it, and the calibration factor is visible
in `livium status`.

### Capability growth is a lifecycle, not a code generator

`request_capability` records a proposal with a reuse analysis (direct reuse →
composition → extension → new code) and a verification plan. `capability scaffold`
creates a runnable skeleton; `capability test` runs its tests in a separate process.
Two rules make the lifecycle honest:

- **A skeleton cannot validate itself.** Tests that only assert the scaffold's
  behaviour would otherwise reach `validated`. The harness refuses to validate a
  module that still contains scaffold markers, keeps the status at `experimental`, and
  journals `capability_test_refused`.
- **Only a human promotes.** `promote --approved-by "Name"` requires `validated`
  status and passes the guardian gate; it copies the module into `livium/promoted/`,
  where it is loaded at the next startup.

Rejection and rollback are first-class: a capability whose tests fail cannot be
promoted, and a promoted one can be rolled back with its record intact.

### The guardian protects its own inputs

Protected files (`livium/prompts/*.md`, `AGENTS.md`) are hashed into
`guardian_baseline`. The engine denies the agent write access to them, denies writes
to the baseline, and `frozen` mode blocks promotion. `livium guardian status` reports
the enforcement rules *and* what those rules cannot stop: a process with the same
filesystem access as the agent could rewrite the database. Claiming otherwise would
be exactly the kind of empty assurance the specification forbids — so the mechanism is
described by its real strength, and `tests/test_guardian.py` verifies tampering is
detected rather than pretending it is impossible.

### Recovery inspects; it does not replay

`recovery.py` reads the task's persisted state, then looks at the world: for each
unconfirmed intent it checks the artifact (existence, size, sha256) and classifies the
outcome as `confirmed_complete`, `confirmed_absent`, `indeterminate`, or `conflict`.
Resume safety follows from that classification. Demonstration D shows the interesting
case: an interrupted write that *did* land is recognised as complete — not retried,
because retrying would double-apply it.

## Front end

`server.py` serves a static page and a JSON API over the same store, so the viewer
cannot show anything the agent does not actually have. `web/app.js` is an ES module
with no build step. Every field it reads exists in the API response, and that is
checked by tests: the viewer displays real `context_report`, `permission_decision`,
`guardian_baseline`, `capability` and `verification` rows rather than invented
dashboards.

## Why Python, and why no other language

The decision was deliberate and is recorded here because the specification requires
justification rather than preference. Python 3.11 was chosen because:

- `sqlite3`, `unittest`, `http.server` and `subprocess` cover the entire requirement
  with **zero** dependencies, and the whole system stays inspectable.
- The agent's real bottlenecks are model latency and judgement, not CPU. A compiled
  component would add a toolchain, a build step, and an FFI boundary for no measurable
  capability — a complexity cost the specification explicitly forbids.
- Deterministic, dependency-free tests are possible, which is what makes the honesty
  claims in this document checkable.

The front end is plain JavaScript for the same reason: no build pipeline means no
divergence between what is served and what is reviewed. If a future requirement is
genuinely compute-bound (for example, local embeddings), it should be added as a
measurement-backed decision — not as architecture in advance.
