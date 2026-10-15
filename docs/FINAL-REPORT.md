AGENT LIVIUM — FINAL REPORT
generated 2026-10-15T07:18:40+0000
repository /home/user/Livium
home /tmp/live
provider openai_compatible:gpt-4o-mini (probe: openai-compatible@https://api.openai.com/v1 is unreachable at https://api.openai.com/v1: TLS/SSL connection has been closed (EOF) (_ssl.c:992). Check network egress and LIVIUM_BASE_URL.)

 1. WHAT WAS DELIVERED
    A working agent runtime in the Python standard library: task loop, tool layer, permission boundary, persistent memory, context management, durable task state, recovery, verification, capability lifecycle, growth guardian, HTTP API and viewer.
    1 capability record(s) exist; 1 are active: memory_recent.
    12 tools are callable right now.

 2. HOW TO RUN AND CHECK IT
    `python3 -m livium doctor` — what works here, what does not, and why.
    `python3 -m livium serve --port 8787` — live state: journal, memory, context reports, capabilities, permission decisions.
    `python3 -m unittest discover -s tests -t tests` — the test suite, no network needed.
    `python3 demos/run_demonstrations.py` — the six mandatory demonstrations, re-runnable from a fresh clone.

 3. WHAT IS VERIFIED, AND HOW
    211 test methods across 14 modules; each asserts observed behaviour against real storage and real subprocesses.
    Last recorded full-suite run: 211 tests, exit code 0 (0 min old).
    Journal: 90 events, 0 observed boundary violation(s), 0 refused git operation(s), 24 real provider probe(s).
    Configured boundary: 20 allowed binaries, 52 denied, 0 allowed network host(s).

 4. THE SIX MANDATORY DEMONSTRATIONS
    Recorded outcomes: A1 real provider task execution = blocked by environment; A2 orchestration task via scripted test double = verified; B memory across process restart = verified; C context construction = verified; D recovery after interruption = verified; F rejection and repair = verified; E capability growth = verified (0 min old)
    Recorded demonstration run: 0 min old
    A — real task execution: attempts the configured provider, and reports blocked-by-environment with the exact error when it is unreachable; the same task through the scripted double is labelled as such and never counted as model proof.
    B — memory survives process death: a separate writer process exits, a later reader process retrieves the record, and the CLI search agrees.
    C — context management: the relevant record is included, the irrelevant noise is excluded, and the budget and omissions are recorded in `context_report`.
    D — recovery: an interrupted write is confirmed from the real file (size, sha256) rather than assumed, and the report says which facts remain unknown.
    E — genuine capability growth: the gap was measured (memory_search returns nothing for a recency question while records exist), then proposed, implemented, tested, promoted by a named human, and used by a fresh agent.
    F — controlled rejection: a deliberately broken change fails its tests, promotion is refused, and the repaired change passes.

 5. PERSISTENT MEMORY
    12 record(s) live in SQLite; kinds: {'decision': 1, 'experience': 3, 'failure': 3, 'foundational': 3, 'task': 2}; verification states: {'executed': 2, 'tested': 4, 'unverified': 2, 'verified': 4}.
    Retrieval is ranked full-text search over real rows; every record carries confidence, verification state, evidence, source and timestamps.
    Instructions and their prior versions are preserved in `memory_history`; forgetting retires a record instead of deleting it, so history survives.
    Demonstration B is the durability evidence: it crosses a real process boundary.

 6. CONTEXT MANAGEMENT
    Context is assembled per step from instructions, objective, criteria, relevant memory, capability limits, execution state and unresolved questions, in a fixed priority order with mandatory instruction sections first.
    Budget: window 128000 tokens, output reserve 1024 (window source: built-in table for a known model (the provider's /models endpoint does not report context length)).
    6 context report(s) recorded; each names what was included and what was omitted, so a decision to drop something is auditable.
    Estimates are labelled estimates and calibrated against real provider usage when the provider reports it; a build that cannot fit the mandatory sections refuses rather than truncating a rule.

 7. DURABLE TASK STATE AND RECOVERY
    2 task(s) and 6 step(s) recorded; intents by state: none.
    0 recovery finding(s) journalled. Each is a determination from evidence: confirmed_complete, not_executed, conflicting or unknown.
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
    Verification results recorded: {'failed': 1, 'manual': 1, 'passed': 3}.
    Tasks that cannot run end `blocked` with the real error rather than a substituted answer; the provider probe is real HTTP, not a configuration check.
    Two real defects were found by these tests during development and fixed in the product (an accepted-but-ignored per-call timeout, and a scaffold that could validate itself); both are now pinned by regression tests.

12. PLANNED, AND DELIBERATELY NOT DONE
    Live inference against a hosted provider: no route to api.openai.com / api.anthropic.com / any Ollama from this build environment; the clients, their wire formats and their failure paths are tested, the success path is not observed here
    Filesystem confinement for subprocesses: requires a container or a mount-namespace/overlay setup; not attempted rather than faked, see docs/LIMITATIONS.md section 2
    Semantic memory retrieval: needs an embeddings provider or a dependency; keyword FTS5 was sufficient for every observed need so far, and invention by default is what the specification forbids
    Concurrent task execution: single-writer SQLite design; parallel agents would need coordination that no observed requirement justified
    Instruction self-modification: intentionally proposal-only: a human approves instruction changes

Read docs/LIMITATIONS.md for the boundaries this report cannot cross.
