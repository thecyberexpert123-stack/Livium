# Limitations — what Agent Livium is not

This file exists because a system that hides its own boundaries invites people to
trust it past them. Everything here is a real constraint of the implementation, not a
roadmap item dressed as modesty. Where a limitation has a test or a command that
demonstrates it, that is named.

## 1. No hosted model has been reached from the environment this was built in

The build environment has no route to `api.openai.com`, `api.anthropic.com`, or any
local Ollama. `https://api.openai.com/v1/models` fails with `EOF occurred in violation
of protocol` (TLS handshake cut). Consequently:

- **Not verified by observation:** a completion produced by a hosted model driving a
  task end to end. Demonstration A is recorded as *blocked by environment*.
- **Verified:** the HTTP requests the clients build (method, path, headers, JSON body)
  are asserted against a real `http.server` in the tests; a real connection attempt is
  made and its failure is surfaced honestly; the scripted test double exercises the
  loop, tools, verification, recovery and persistence deterministically.
- **What this means for you:** run `livium doctor` on a machine with egress before
  trusting the model path. The first thing it does is probe the provider and tell you
  whether it is usable.

A task whose provider is unreachable ends `blocked` with the error attached. It never
reports success, and it never falls back to a canned answer.

## 2. Command execution is not a security sandbox

`livium/execution.py` runs commands through `unshare -rn` (network + user namespace)
and kills the whole process group on timeout. That is real but partial:

- **Network: isolated** (verified at runtime by executing a probe inside the namespace).
- **Process: namespaced.**
- **Filesystem: NOT confined.** A launched program runs as your user and can read
  anything your user can read, including `~/.ssh`, `/etc`, and this repository. The
  workspace directory is where the *agent* is permitted to write; it does not stop a
  program from writing elsewhere.
- **No seccomp, no chroot, no container, no cgroup memory limit.** A command can
  consume CPU and disk until the timeout kills its process group.
- A child that deliberately creates a new session to escape the process group is not
  guaranteed to be killed.
- `unshare` requires Linux with user namespaces enabled. Where it is unavailable, the
  isolation check fails and is reported as unavailable — the tool does not silently
  downgrade to running commands unconfined while claiming isolation.

No component of this project is named `Sandbox`, because none of them is one. The
defence here is the allow-list of binaries plus the fact that the operator chooses to
run the agent at all.

## 3. The guardian detects tampering; it does not prevent a determined operator

`guardian_baseline` records a SHA-256, size, mtime, approver and note for each
protected file (`livium/prompts/*.md`, `AGENTS.md`). The agent's tools cannot write
those files, cannot modify the baseline, and cannot re-baseline or promote under a
frozen guardian.

What it cannot do:

- Prevent a **human** or any other process with write access to `LIVIUM_HOME` from
  altering both a protected file and the baseline. It is a tamper-*evident* control
  for the agent's own operation, not an access control against the operator.
- Survive deletion of the database, which takes the baseline with it.
- Defend against the agent manipulating *inputs* (documents, command output) to
  influence later decisions. Files are untrusted input here, including `AGENTS.md`.

`livium guardian status` prints these enforcement limits alongside the rules, so the
guarantee is never mistaken for something stronger.

## 4. Isolation of the agent's runtime from its own instructions is procedural

The agent is instructed by protected prompts, and the guardian detects edits to them.
But the agent still *reads* files it is told to read, and a sufficiently adversarial
file is an adversarial file. Prompt-injection resistance here rests on: instructions
take priority over retrieved content, high-impact actions are permission-checked
mechanically (not by model judgement), and irreversible operations (promotion,
instruction changes) require a human. There is no formal guarantee of
non-manipulability, and no claim of one.

## 5. Context management uses estimates, not a tokenizer

No dependency-free tokenizer matches a given provider's exact tokenization. Livium:

- takes the context window from an explicit `LIVIUM_CONTEXT_WINDOW` if set, else the
  provider's `/models` data **where the endpoint actually reports it** (most do not),
  else a built-in table, else a conservative 8192;
- estimates prompt tokens with a characters/4 heuristic, **calibrated** by comparing
  estimates against real usage figures when the provider returns them;
- reserves output space from `max_output_tokens` and refuses to build a context that
  cannot hold both the reserve and the mandatory instruction sections.

A build that refuses can produce a `blocked` task. That is intentional: refusing is
correct, silently truncating a rule is not. The heuristics' accuracy bounds are
unknown until calibrated against a real provider's reported usage.

## 6. Memory retrieval is keyword-based, not semantic

FTS5 over the record text, plus trust and recency signals. Paraphrase without shared
terms will not be found — a question like *"what did I learn recently?"* returns zero
rows from `memory_search` unless the words appear in a record. This is a real
limitation, and it is also how the agent's first genuine capability gap was found: the
missing ability to enumerate by recency was implemented (`memory_recent`) rather than
hidden by loosening the query. Embeddings would require either a provider call or a
dependency; neither was justified by evidence at the time.

## 7. Instruction self-improvement is proposal-only by design

The agent records lessons and can propose changes to its own instructions with
rationale and evidence. It **cannot** apply them: a human reviews and approves, and
prior versions are preserved in `memory_history`. A single failure never triggers an
automatic rewrite. This is a deliberate constraint, not an unfinished feature — it is
also a limitation if you were hoping for unattended self-modification.

## 8. Tasks run one at a time

The loop is single-threaded per process. SQLite is used in WAL mode with busy
timeouts, which is safe for concurrent readers (`livium serve`) and a single writer,
but two agents writing the same `LIVIUM_HOME` concurrently is not a supported
configuration. There is no distributed coordination, no queue, and no resumption of a
task in a different process while the first still runs.

## 9. Promoted capabilities load at startup

Promotion copies a validated module into `livium/promoted/` and it is callable from
the next agent startup. There is no hot reload; a long-running agent does not pick up
a promotion without restarting. Rollback is likewise a file operation plus restart.

## 10. The viewer is a local operations console

It binds to the host you specify, has **no authentication**, and exposes the agent's
journal and memory. Run it on `127.0.0.1` unless you have put your own authentication
in front of it. It is served by the same stdlib HTTP server as the API; it is not
hardened for hostile networks, and it has not been tested for accessibility or
against older browsers. Long-poll-free: it polls the API on an interval.

## 11. Scope of the tests

`python3 -m unittest discover -s tests -t tests` runs the suite with no network and no
credentials, which is what makes it reproducible. That also means the suite cannot
verify provider behaviour that only a live endpoint exhibits (rate limits, tool-call
quirks, token accounting). Those paths are written defensively and their error
handling is tested, but the success paths against real providers are unverified here.

Where a test uses a double, it is declared as one: the scripted client is refused
unless `LIVIUM_ALLOW_TEST_DOUBLE=1` is set, results carry `is_test_double`, and the
CLI prints a warning. No verification result in this repository is produced by a mock.

## 12. A promotion proved nothing until the tests stopped lying

The capability lab ran a scaffold's tests and reported them passing. The scaffold's
test file contained no assertions and the module returned an empty `ToolResult` for
every call, so "tests passed" and "the tool does nothing" were both true at the same
time. Promotion was therefore reachable from a placeholder -- the lifecycle looked
enforced and was not.

Promotion now requires evidence that could have failed: at least one test that
executes the capability and asserts on its output, and an interface that is not the
scaffold's placeholder. `capability test` refuses the generated stub, and a regression
test pins it (`tests/test_capabilities.py::PlaceholderTests`). The general lesson is in
`AGENT-EXPERIENCE.md`: a check that cannot fail is not a check.

## 13. The guardian's own instructions are the agent's, not the model's

`livium/prompts/*.md` are protected by the permission engine (write refused, hash
baselined) but they are still *text in a prompt*. The enforcement that matters is the
harness: `TaskRouter` decides the step, `ToolRegistry` authorises each call, and
`Verification` executes the criteria. If a prompt were replaced, the agent would keep
refusing what the engine refuses. What a modified instruction file could change is
judgement — which of two permitted actions is chosen — and that is stated rather than
smoothed over.

## 14. What mission test H does and does not prove

Test H shows the model's recommendation has no effect on a refusal: the call is denied,
the denial is journalled with its reason, and the run continues or stops according to
the router. It does not prove the *permission rules themselves* are correct -- a rule
that wrongly denies something is still enforced, and the specification asks for
enforcement, not for omniscience about which rule is right.

## 15. The integrity layer detects and records; it does not stop a privileged process

The write refusal, the guardian and `livium.integrity` sit *inside* the runtime they are
protecting. An agent that can obtain an operating-system process with the same
privileges -- which `run_command` with an allowlisted interpreter can, and which is the
whole point of having a runner -- can modify a protected file, delete the snapshot table,
or rewrite the record by going around the Python API entirely. What the layer guarantees
in that case is not prevention. It is that the change is *detected* by digest on the next
check, recorded in an append-only table, attributed as far as the evidence goes, and made
visible to a human, and that the run freezes rather than continuing as though nothing
happened. `docs/LIMITATIONS.md` says "detects" rather than "prevents" deliberately.

Two mechanisms reduce the window rather than closing it: the enforcement probes run in a
separate interpreter against a copy of the tree, so the code being checked does not get to
report on itself, and `integrity_event` refuses `UPDATE` and `DELETE` at the SQLite level
rather than relying on the application not to issue them. Neither survives an attacker
with write access to the database file. The honest claim is therefore: **a tamper is
recorded and visible, not impossible.**

## 16. What the enforcement probes cover, and what they do not

`livium integrity verify` runs eleven probes: a protected write is refused, a write
outside the permitted roots is refused, a shell interpreter is refused, a non-allowlisted
binary is refused, network access is refused with an empty allowlist, the promotion gate
blocks after a boundary violation, an anonymous approver is refused, a modification is
detected, a clean tree is accepted, the record refuses an update and a delete, and a
baseline can be recorded. Each probe asserts a refusal or a detection; a candidate that
removes the ability to notice a change fails the detection probe, and a test asserts that
it does (`tests/test_integrity.py::EnforcementProbeTests`).

They do not prove the absence of every weakening. A candidate that keeps each asserted
behaviour but narrows what is *checked* -- say, hashes a prefix of each file -- passes
them. A candidate could also weaken something none of the probes observes. The probes are
evidence about the behaviours they name; they are not a proof about the system as a
whole, and the review that accompanies them is a human reading a structural diff.

## 17. The discovery queue's judgement is lexical, and a merge can still be wrong

Two records are treated as the same observation when their normalised text is identical,
or when the same source is named and at least three significant words are shared and the
token overlap is at least 0.6. That is a heuristic: a genuinely different observation that
shares enough wording with an existing one will be *merged*, and its evidence appended to
the earlier record rather than kept as a separate row. The merge never overwrites the
original provenance, and it appends history, so nothing is silently lost -- but the second
observation does not get its own state, its own ranking or its own investigation.

Ranking is lexical too: confidence, evidence, risk and effort, combined by a fixed
formula, with no learning from outcomes. A queue that has been reviewed by a human is the
input to the next ranking only to the extent that statuses differ.

## 18. A follow-up conclusion is a claim with verified evidence, and both are labelled

When a follow-up investigation closes an item as `completed`, the record contains the
harness's verified evidence (which task ran, which tools executed, which criterion
passed) *and* the conclusion the task reached. The harness verifies that the investigation
touched real state; it cannot verify that a sentence about the world is true. The two are
stored in separate fields and the reason text says which is which, precisely so that a
later reader cannot mistake the conclusion for a verified fact.

