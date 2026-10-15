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
