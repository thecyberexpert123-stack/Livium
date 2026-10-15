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
reports success, and it never falls back to a canned answer. Demonstration A1 now
attempts the *task*, not just a probe, through that same real provider; A2 is a
separately labelled scripted run. Older A1 evidence had mistakenly used the scripted
client for its task after a failed real probe, so it has been replaced.

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

## 9. Promoted capabilities load at task startup, not mid-task

Promotion atomically copies the *whole tested package* (entrypoint, helpers, assets and
tests) to `$LIVIUM_HOME/promoted/NAME/`, by default. Its database row records the
sha256 of every file; a new agent process checks the active status and every digest
**before importing** any code. A naked file dropped into the directory or a package
whose bytes changed after promotion is refused and reported. A long-running process
that already imported a tool does not hot-reload a promotion or rollback: restart it.
The Studio starts a fresh `livium run` process for each task, so its *next* task sees
the new set; restart `livium serve` to refresh its separate read-only tool catalog.

**Migration:** a pre-0.5 installation may have a flat `livium/promoted/NAME.py` and
no file-manifest columns. This version will **not import it**, even on the old home, because
its code is not tied to the old test result. The human must copy or inspect its
source and tests in the lab, re-run `capability test`, and promote it again to the
per-home directory. The old file is not silently trusted, deleted or migrated.

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

The test runner refuses the generated implementation while scaffold markers remain,
requires at least one `test_*.py`, actually runs unittest, checks the declared tool
schema in a separate limited subprocess, and ties **all** lab files to their digests.
The tests in this repository call the capability and assert an observed output; the
runtime cannot prove that an arbitrary future author's assertions are meaningful.
An author who writes `assertTrue(True)` can get a green test result, but not an
objective proof of behaviour. The human review is therefore a real responsibility,
not a checkbox outsourced to the model.

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

## 19. What the follow-up budget can promise, and what it cannot

A follow-up cycle's limits are enforced by charging the budget *before* the work: a model
call, a step, a command execution. When a charge is refused the work does not happen, the
item returns to `ready`, and the limit's own name is recorded. That much is a guarantee, and
`tests/test_discovery.py` asserts it against the real loop, not against a stand-in.

Three things it does not promise:

**The token limit cannot stop the call that overspends it.** A reply's size is unknown until
it arrives, so measured usage is charged after the fact and the limit stops the *next* call.
The ceiling on one reply is the provider's own output limit, which Livium configures but does
not enforce. Where the provider reports no usage at all, `tokens_are_measured` stays false and
the token limit is an estimate limit; the record says so instead of implying a measurement.

**The time limit is checked at charges, not continuously.** The deadline is read before each
model call, step and command, so a single long-running command is bounded by the *command*
timeout (`LIVIUM_MAX_COMMAND_SECONDS`), not by the cycle's remaining seconds. A cycle stops
promptly between units of work, not during one.

**A stopped investigation is not a conclusion.** The record keeps what the attempt established
and stays `ready`, and it is not retried automatically: it waits for a later cycle, a human, or
the next task that notices the same thing. The ledger reports these attempts in their own
column (`followups_stopped`) rather than counting them as investigations that concluded, which
means the two numbers have to be read together.

## 20. The Studio is a console with no authentication, and its transcript is second-hand

**What it is.** `ui/` serves a page, proxies the runtime's read-only API, and starts
`livium run` for a task. It holds no agent logic: every permission decision, verification
and journal row it displays was produced by the Python runtime.

**What it does not protect.** There is no login, no session and no CSRF token beyond a
required `X-Livium-Studio` header on writes (which stops a cross-site form, not a local
process). Anything that can reach the port can start a task, read the workspace and
delete files in it, so the default binding is `127.0.0.1` and `--host 0.0.0.0` is an
explicit, warned-about act. `settings.json` is mode 0600 and the Studio's HTTP
workspace browser refuses it for preview, raw download, overwrite or deletion (a
regression test covers all four). API responses give only a key hint; extra
cloud/environment values are write-only in the settings response (their names remain
visible). The proxy redacts known keys and long configured values from JSON replies,
including escaped values, but short extra values in arbitrary task data cannot be
reliably scrubbed without corrupting ordinary content. A separate workspace file
containing a copied secret is still a downloadable file: do not put secrets in tasks
or workspace artifacts. This is *not* filesystem isolation: arbitrary Python run as
the same OS user may still read the key from its file. And a caller who can reach
the port can spend it by starting tasks.

**Where the transcript can be incomplete.** `lib/session.mjs` renders only the events the
runtime journalled. A task that predates the journal, a run interrupted before any event
was written, or a message the runtime chose not to journal appears as nothing — the panel
says a task has no records rather than filling the gap. Two further limits: the transcript
shows the task row's objective while a resumed task may have been started with a different
one (the difference is shown, not resolved), and the token figures are the provider's own
report when there is one and a labelled estimate otherwise.

**What "verified" means in the UI.** It is the runtime's verdict on the acceptance
criteria, not a judgement of the work's quality: a criterion can be weak, and a task with
no criteria is shown as *unverified* for exactly that reason. The Studio does not add a
criterion, and nothing it displays should be read as one.

## 21. Authored Python is not a security sandbox; named-human promotion is an operational gate

The agent can create a lab, author a Python module with helpers and data, write tests,
and invoke the test runner itself. The validated state now binds the entire lab to
SHA-256 digests, and promotion refuses any change after the tests; the loader refuses
unregistered, rolled-back or changed packages before import. Integration tests run
this path from a task through the Node console, a human CLI call, a second task that
uses the tool, then named-human deprecation and rollback. Those are real
filesystem/SQLite/subprocess actions, but the model's chosen calls are **scripted
test-double replies**. No real provider has
driven a capability-development task in this environment.

`Runner` can verify network isolation here but reports `filesystem_confined=False`.
A `run_command` or a capability's tests can execute arbitrary Python with the OS
user's filesystem access; promoted Python also executes in-process with the same
privileges as the agent. A command could write the SQLite DB or promoted files
without going through the tool or human gate. The CLI's `--approved-by` string is
**attribution**, not authentication of a human identity. These facts are why a
manifest check is an *integrity check*, not a claim of resistance to malicious code
running as the same user. Do not run untrusted modules, or put a real API key in a
home whose same-UID Python you don't trust. Use a separate unprivileged OS identity,
a real filesystem sandbox and an authenticated out-of-band operator boundary if you
need a hard guarantee against an adversarial model. The Studio offers no promotion
HTTP route because, with no login, a button labelled “human approve” would be false.

A successful test is also not proof that the generated capability is correct,
safe, or useful on other inputs. It proves the executed assertions for the recorded
bytes. The human must examine the tests and code before activating them. The host
still needs a reachable model provider and a locally configured key for a real
model to attempt this workflow.
