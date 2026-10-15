written: docs/evidence/final-report.json
AGENT LIVIUM — FINAL REPORT
generated 2026-10-15T08:24:44+0000
repository /home/user/Livium
home /tmp/livium-release
provider openai_compatible:gpt-4o-mini (probe: openai-compatible@https://api.openai.com/v1 is unreachable at https://api.openai.com/v1: TLS/SSL connection has been closed (EOF) (_ssl.c:992). Check network egress and LIVIUM_BASE_URL.)

 1. WHAT WAS DELIVERED
    A working agent runtime in the Python standard library: task loop, tool layer, permission boundary, persistent memory, context management, durable task state, recovery, verification, capability lifecycle, growth guardian, HTTP API and viewer.
    1 capability record(s) exist; 1 are active: memory_recent.
    13 tools are callable right now.
    Task-directed flow runs inside the existing loop: a deterministic router decides each step (livium/router.py), the context manager carries a compact tool catalog instead of every interface (livium/context.py), and one learning-ledger row per task records what was measured (livium/ledger.py).

 2. HOW TO RUN AND CHECK IT
    `python3 -m livium doctor` — what works here, what does not, and why.
    `python3 -m livium serve --port 8787` — live state: journal, memory, context reports, capabilities, permission decisions.
    `python3 -m unittest discover -s tests -t tests` — the test suite, no network needed.
    `python3 demos/run_demonstrations.py` — the six mandatory demonstrations, re-runnable from a fresh clone.

 3. WHAT IS VERIFIED, AND HOW
    289 test methods across 19 modules; each asserts observed behaviour against real storage and real subprocesses.
    Last recorded full-suite run: 289 tests, exit code 0 (1 min old).
    Journal: 122 events, 0 observed boundary violation(s), 0 refused git operation(s), 8 real provider probe(s).
    Mission acceptance tests: A retrieval, B budgeting, C capability discovery, D feedback recovery, E cross-process reuse, F genuine growth, G token accounting, H permission enforcement — test_approval 17, test_hygiene 3, test_ledger 13, test_mission 18, test_router 24 test(s), all passing in the recorded run.
    Configured boundary: 20 allowed binaries, 52 denied, 0 allowed network host(s).

 4. THE SIX MANDATORY DEMONSTRATIONS
    Recorded outcomes: A1 real provider task execution = blocked by environment; A2 orchestration task via scripted test double = verified; B memory across process restart = verified; C context construction = verified; D recovery after interruption = verified; F rejection and repair = verified; E capability growth = verified (1 min old)
    Recorded demonstration run: 1 min old
    A — real task execution: attempts the configured provider, and reports blocked-by-environment with the exact error when it is unreachable; the same task through the scripted double is labelled as such and never counted as model proof.
    B — memory survives process death: a separate writer process exits, a later reader process retrieves the record, and the CLI search agrees.
    C — context management: the relevant record is included, the irrelevant noise is excluded, and the budget and omissions are recorded in `context_report`.
    D — recovery: an interrupted write is confirmed from the real file (size, sha256) rather than assumed, and the report says which facts remain unknown.
    E — genuine capability growth: the gap was measured (memory_search returns nothing for a recency question while records exist), then proposed, implemented, tested, promoted by a named human, and used by a fresh agent.
    F — controlled rejection: a deliberately broken change fails its tests, promotion is refused, and the repaired change passes.
    Mission demonstrations (sections 12): M1 selective retrieval = verified; M2 capability discovery = verified; M3 failure changes the next action = verified; M4 resume after interruption = verified; M5 experience reuse across processes = verified; M6 token accounting = verified (1 min old)

 5. PERSISTENT MEMORY
    40 record(s) live in SQLite; kinds: {'decision': 3, 'experience': 29, 'failure': 2, 'foundational': 3, 'task': 3}; verification states: {'claimed': 2, 'tested': 2, 'unverified': 26, 'verified': 10}.
    Retrieval is ranked full-text search over real rows; every record carries confidence, verification state, evidence, source and timestamps.
    Instructions and their prior versions are preserved in `memory_history`; forgetting retires a record instead of deleting it, so history survives.
    Demonstration B is the durability evidence: it crosses a real process boundary.

 6. CONTEXT MANAGEMENT
    Context is assembled per step from instructions, objective, criteria, relevant memory, capability limits, execution state and unresolved questions, in a fixed priority order with mandatory instruction sections first.
    Budget: window 128000 tokens, output reserve 1024 (window source: built-in table for a known model (the provider's /models endpoint does not report context length)).
    8 context report(s) recorded; each names what was included and what was omitted, so a decision to drop something is auditable.
    Estimates are labelled estimates and calibrated against real provider usage when the provider reports it; a build that cannot fit the mandatory sections refuses rather than truncating a rule.

 7. DURABLE TASK STATE AND RECOVERY
    4 task(s) and 9 step(s) recorded; intents by state: {'done': 2}.
    1 recovery finding(s) journalled. Each is a determination from evidence: confirmed_complete, not_executed, conflicting or unknown.
    Recovery never replays blind: an operation that landed is not repeated, and an operation whose effect is unobservable is reported as unknown with the argv it ran.

 8. CAPABILITY GROWTH
    Lifecycle: proposed → experimental → validated → active → maintained → deprecated, with rollback and rejection as first-class outcomes.
    A scaffold cannot validate itself: a module containing TODO/NotImplemented markers is refused, its status stays experimental, and the refusal is journalled.
    Only a human promotes: `--approved-by` is required, the guardian gate runs, and the module is installed into `livium/promoted/` where it loads at the next startup.

 9. GROWTH GUARDIAN AND COMPLEXITY CONTROL
    Guardian integrity: ok — 8 protected file(s) match the recorded baseline
    Protected paths checked: 8; mismatches: 0; missing baselines: 0.
    The agent cannot write protected paths, cannot re-baseline, and cannot promote while the guardian is frozen; those are refusals in the permission engine, not conventions.
    Complexity control is applied by deletion: unreachable branches, unused parameters, unused imports and dead API surface were removed rather than kept 'in case'.

10. SECURITY BOUNDARY
    Commands: 20 allow-listed binaries, 52 denied; no shell interpreter is permitted, so shell syntax stays literal text.
    Network: 0 allowed host(s); the runner's isolation mechanism is unshare-netns (network isolated: True, filesystem confined: False).
    Filesystem: writes are confined to the workspace and the capability lab by the permission engine; subprocess filesystem access is NOT confined, and that is stated rather than implied (docs/LIMITATIONS.md section 2).
    Credentials: the child process environment is built from scratch, so the agent's own environment — and any API key in it — is never inherited by a command.

11. HONESTY OF VERIFICATION
    A model's completion claim does not close a task: acceptance criteria are executed by the harness, and unmet criteria are reported as a partial result with the remaining criteria named.
    Verification results recorded: {'passed': 3}.
    Tasks that cannot run end `blocked` with the real error rather than a substituted answer; the provider probe is real HTTP, not a configuration check.
    Six product defects were found by these tests and fixed in the product rather than worked around in a test, each now pinned by a regression test: a scaffold that could validate itself; a per-call command timeout accepted and ignored; an explicit numeric zero silently rewritten to the default; a missing working directory reported as 'executable not found' (exit 127); a runner error path that raised TypeError instead of returning its result; and a viewer reading status.permissions from a payload that never contained it. Details in CHANGELOG.md.

12. PLANNED, AND DELIBERATELY NOT DONE
    Live inference against a hosted provider: no route to api.openai.com / api.anthropic.com / any Ollama from this build environment; the clients, their wire formats and their failure paths are tested, the success path is not observed here
    Filesystem confinement for subprocesses: requires a container or a mount-namespace/overlay setup; not attempted rather than faked, see docs/LIMITATIONS.md section 2
    Semantic memory retrieval: needs an embeddings provider or a dependency; keyword FTS5 was sufficient for every observed need so far, and invention by default is what the specification forbids
    Concurrent task execution: single-writer SQLite design; parallel agents would need coordination that no observed requirement justified
    Instruction self-modification: intentionally proposal-only: a human approves instruction changes

13. TASK-DIRECTED INFORMATION FLOW: WHAT WAS MEASURED
    Router decisions recorded in this home: execute 6, verify 2, retrieve_context 1. The router is a deterministic state machine; the model recommends and the harness enforces.
    Tool interfaces sent per step: 7-7 of 13 available, selected from the objective and the state of the run; the compact catalog is what makes every interface unnecessary.
    Ledger: 3 task row(s). Actual token usage was reported for 0 of the last 3 task(s); where usage was not reported the column is NULL and the estimate is stored separately, never merged with it.
    No operation has needed human approval in this home; the queue, the CLI decision path and the one-decision-one-execution rule are covered by tests.
    Promoted capability tools: memory_recent.
    Not verified here: real-provider token counts. No hosted provider is reachable from this environment, so every actual token number that exists was produced by a transport that reports usage; the code path for real providers is the same one.

Read docs/LIMITATIONS.md for the boundaries this report cannot cross.
