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
  integrity.py     the protection policy, the append-only integrity record, snapshots,
                   structural review, approval, verified recovery, enforcement probes
  discovery.py     deferred observations: the queue and its transition table, provenance
                   and untrusted-content flags, the follow-up budget and cycle
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
ui/                the Studio: an operator console, in Node, with no dependencies
  server.mjs       static files, the console API, and a same-origin proxy to `livium serve`
  lib/paths.mjs    workspace containment (textual *and* after symlinks are followed)
  lib/settings.mjs the key and the settings file: 0600, one-way, never echoed
  lib/session.mjs  journal events -> a transcript, a verdict and an artifact list
  lib/backend.mjs  the child processes: `livium serve` and one `livium run` per task
  public/          the page itself (no framework, no build step)
  test/            node --test: containment, the key, the run, the transcript, the contract
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

### Deferred discovery: the observation is not the task

A task that notices something else worth doing has two bad options -- chase it, and
derail the work it was asked to do, or drop it. Livium takes neither. `record_discovery`
is offered on every step, so the observation is captured while it is fresh; the queue is
durable, so it survives the task and the process; and the *task* continues unchanged. It
is not a to-do list for the task in progress: the capture tool returns a handle, not an
instruction, and nothing about the primary task depends on the queue being empty.

What happens to the queue is decided by a router rule that only fires in one state: the
harness has verified the task's acceptance criteria, the model has asked to finish, and
there are ready records. It returns `evaluate_discoveries`, which names the queue in the
task's record and hands it to a follow-up cycle with its own budget.

Investigating an item is a *task*: same loop, same tools, same permission checks, its own
task id, its own acceptance criterion (it must have touched real state) and its own
verification. The conclusion a follow-up records is labelled as the task's own claim and
is stored next to the evidence the harness did verify. When the budget runs out the cycle
stops, says why, and leaves the rest of the queue ready -- a queue that reopens its own
items is a queue that never drains.

Provenance is part of the record, not a comment on it. Text read out of a file is stored
as `observed_content` and flagged; it can be evaluated, and it can never be mistaken for
an instruction the agent gave itself.

### The follow-up budget: charged by the work, not reported about it

A budget that is checked between items is a budget that describes the last item, not one
that bounds the next. So the counters live on `FollowUpBudget` and the work charges them:

```
FollowUpCycle.run(...)                      # owns max_items and the deadline
  └── runner(record, budget)                # one investigation
        ├── budget.spend("steps", 1)        #   before each command it executes
        └── agent.run_task(..., spend=budget.spend)
              ├── budget.spend("steps", 1)        # before each loop step
              └── agent._call_model(..., spend=budget.spend)
                    ├── budget.spend("model_calls", 1)   # before each provider attempt
                    └── budget.spend("tokens", reported) # after the reply, from its usage
```

`spend()` raises `BudgetExhausted` instead of returning `False`, because the caller is about
to do something with a cost and a return value is easy to ignore. The exception carries the
limit's name, and the loop turns it into a clean stop: the task keeps everything it did,
records `task_stopped`, and `TaskRun.stopped_because` says which limit ended it. The cycle
then returns the item to `ready`, journals `discovery_investigation_stopped`, and stops.

Two rules keep this from becoming a second execution path. Nothing charges a task that did
not come from a cycle: the default callback is `no_spend`, so an ordinary task cannot be
refused by a budget it does not have. And a stop is never reported as an outcome: an
investigation that ran out of budget did not conclude, so the record stays `ready`, the
ledger counts it in `followups_stopped` rather than `followups_investigated`, and the
report prints both numbers next to the queue's own statuses.

## Integrity: a detection is a record, a recovery is verified

The guardian's job is to notice that a protected component changed. `livium.integrity`
is what happens next, and its first rule is that noticing is not enough: a detection
becomes a row in `integrity_event`, which is append-only **by SQLite trigger**. The row
carries what was seen, the expected state, the policy applied, the decision, who
authorised it, the evidence, any recovery and its outcome, the residual uncertainty, and
as much attribution as the evidence supports -- which is often less than a name.

Recovery is never a claim. `restore` rewrites a protected file from the snapshot of its
baselined digest, atomically, and then hashes what is on disk. If the hash does not match
the baseline the recovery is recorded as failed and an error is raised; there is no path
in the code that reports a recovery it did not verify. Approving a change is the mirror
image: a named human, every weakening finding acknowledged, the behavioural probes
passing against a copy with the candidate applied -- and only then a re-baseline of that
single file.

The probes are what make the enforcement claims behavioural. They run in a separate
interpreter, in their own home, against a copy of the tree, and they tamper with a file
they own: a check that modified the checkout to test detection would be doing the thing
it exists to catch. `tests/test_integrity.py` hands the verifier a deliberately weakened
candidate and asserts that the probes fail, so the probes cannot pass by construction.

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

The API is read-mostly on purpose: state, memory, the journal, context reports,
capabilities, tasks, deferred work and integrity are readable, and the operations that
change a baseline, approve a protected change or promote a capability are **not**
reachable over HTTP at all. Those are the controls that only mean something when a named
human performs them, so they live on the CLI and the server tests assert their absence.

`/api/discovery` returns the queue, the records, and the ranking *with its reasons* --
a score without its terms is a number nobody can argue with. `/api/integrity` returns the
watched set with each component's tier and rationale, the current inspection, the
append-only record and the enforcement history. That route inspects with `record=False`:
a viewer that polls every few seconds must not fill an append-only security record with
its own refresh timer, and a test asserts the poll writes nothing.

## The Studio: a console, not a second agent

`ui/` is a Node HTTP server (Node 20+, zero npm dependencies) that does three things and
nothing else: it serves a page, it proxies `/api/*` to `livium serve`, and it starts
`livium run` as a child process when the operator asks for a task. There is no second
permission engine, no second loop and no second journal. When the page shows a refusal,
a verification state, a written file or a token count, that value came over HTTP from the
Python runtime or out of the runtime's database — the transport is the only thing the
Studio adds.

This is a design constraint with consequences that are visible in the code:

- **The API key goes one way.** It is typed into the page, written to
  `<studio home>/settings.json` with mode 0600 in a 0700 directory, and passed to child
  processes as `LIVIUM_API_KEY` through the environment. Responses carry `api_key_set`,
  `api_key_length` and a hint; the key itself is never returned, never logged, and is
  redacted from any outgoing string. A test asserts a saved key does not appear in the
  state payload.
- **Two halves of a path check.** Every workspace path is checked textually *and* after
  following symlinks, because a link inside the workspace that points at `/etc/passwd`
  passes the first check. Both refusals are tested.
- **The transcript is derived, never narrated.** `lib/session.mjs` maps journalled events
  to messages, artifacts and refusals. A task with no records produces an empty panel
  that says so; a task with no acceptance criteria is labelled unverified rather than
  verified-by-default.
- **A poll is not a probe.** The runtime caches its provider probe and reports how old
  the answer is; `probe=1` asks for a fresh one. Without this, a page polling every eight
  seconds would turn a timer into a stream of provider requests and journal rows — and the
  "provider verified" badge would be measuring the browser.
- **Localhost by default.** The console has no authentication and can write into the
  workspace, so widening the binding is an explicit flag, and the banner says so.

The Studio never holds state that the runtime can contradict: it keeps the last
`livium run` child per task so it can show a log and stop a task, and everything else is
fetched.

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
