# Agent Experience

Lessons recorded while building Agent Livium. Each entry is something that actually
happened in this repository, what it cost, and what changed as a result. This file is
also the source material for the growth-guardian instructions: the rules the agent
operates under were written from these failures, not from imagination.

---

## 1. A scaffold validated itself

**What happened.** `capability scaffold` generates a module and a test that asserts
the generated functions exist. Running `capability test` on the untouched skeleton
passed, moved the capability to `validated`, and prompted a human to promote a module
whose body was `raise NotImplementedError`.

**Why it mattered.** This is the specification's central failure mode — scaffolding
presented as implementation — and the harness itself was producing it. Worse, the
generated test *looked* like verification.

**What changed.** `livium/capabilities.py` gained `placeholder_markers()`. A module
containing `TODO`, `TODO:` or `NotImplemented` cannot be validated: `test()` refuses,
keeps the status at `experimental`, journals `capability_test_refused`, and says
*"Tests cannot validate an unimplemented module, so the status stays experimental
regardless of the test result."* `promote()` refuses for the same reason. Deleting the
placeholder test instead of the placeholder is not a way round it — the check reads
the module, not the test result.

## 2. `args.get("limit") or 10` silently rewrote zero

**What happened.** The first real `memory_recent` accepted `limit`. With `limit=0` it
returned ten rows, because `0 or 10` is `10`. The clamping test caught it on the first
run — nine tests passed, one failed, and the failure was real.

**What changed.** Absent and empty are distinguished from zero before coercion. This
is a general hazard of Python defaults, so the pattern is avoided for all numeric tool
arguments: validate the raw value, then coerce.

## 3. A parameter that could not change anything

**What happened.** The same draft exposed `include_retired`. `Memory.recent()` returns
active records only; retired history is a different query that did not exist.

**What changed.** The parameter was deleted rather than implemented as a lie. A tool
parameter that cannot change behaviour is a false claim about what the tool does.

## 4. "Copy the files in" skipped a lifecycle state

**What happened.** Implementation files were copied into a home whose capability was
still `proposed`, because `scaffold` — the step that moves `proposed` →
`experimental` — had been skipped. `capability test` then failed with
`illegal transition 'proposed' -> 'validated'`.

**What changed.** Nothing in the product: the state machine was right and the
shortcut was wrong. The lesson is about process, and it is why the demonstration
script performs proposal → scaffold → implement → test in order.

## 5. A test installed a file into the repository

**What happened.** A promotion test wrote `livium/promoted/peek.py` into the source
tree. Found later while listing the directory.

**What changed.** Tests redirect `registry.promoted_dir` into a temporary home and
assert the repository is untouched afterwards. A *human-approved* promotion is
supposed to install into `livium/promoted/`; a test doing it is pollution.

## 6. The message a test asserted was produced by `unshare`, not by us

**What happened.** A missing-binary test asserted `"not found" in stderr`. Under
`unshare` the message is `unshare: failed to execute definitely-not-a-real-binary: No
such file or directory`.

**What changed.** The test asserts the binary name appears in the error, and the
product's own message is checked on the path where Livium produces it. Tests should
assert what the system guarantees, not what a wrapper happens to print.

## 7. A permission rule broader than the threat

**What happened.** The permission engine refused any command argument containing shell
metacharacters (`;`, `&&`, `|`, `>`). This blocked `python3 -c "import sys; sys.exit(0)"`
— legitimate, and not a shell, because `subprocess` is called with an argv list and no
`shell=True` anywhere in the codebase.

**What changed.** The metacharacter rule was removed and replaced with one aimed at the
actual risk: `sh`, `bash`, `dash`, `zsh`, `ksh`, `csh`, `tcsh`, `fish` are denied
outright (`cmd`, `powershell`, `rm`, `curl`, `pip`, `sudo` and others are on the
binary deny-list), because *those* turn text into syntax. A test now
proves metacharacters arrive as literal arguments, and another proves the interpreters
are refused. Security rules that are broader than the threat get worked around, and
worked-around rules protect nothing.

## 8. A timeout that was accepted and ignored

**What happened.** `run_command` forwarded a per-call `timeout`, but `Runner.run()`
never read it. A test asking for a 2-second timeout on a 30-second sleep *passed*, and
the assertion that the process had been killed failed — the test took 32 seconds to
notice. The runner was reporting `ok` for a call whose contract it had not honoured.

**What changed.** The timeout is a real parameter: `limit = max(0.5, min(requested,
self.max_seconds))`, used for the wait and named in the message when the process is
killed. A caller can tighten the ceiling, never raise it.

## 9. A confusing failure that pointed at the wrong thing

**What happened.** When the workspace directory did not exist, commands failed with
`exit 127` and an `OSError` naming the *command*, not the missing directory. Tests were
written against that and then had to be rewritten.

**What changed.** The runner creates an absent working directory and reports any other
`OSError` explicitly, so the error names the real problem.

## 10. The tool that returns nothing is sometimes right

**What happened.** A probe asked `memory_search("what did I learn in my most recent
sessions?")` and got zero rows while three records existed. The temptation is to
loosen the query until it matches.

**What changed.** The zero was treated as evidence: no tool could enumerate by
recency, `memory_search` requires a non-empty query, and `sqlite3` is not allowlisted,
so the memory table was genuinely unreachable from the agent's side. That measurement
became the gap in Demonstration E, and `memory_recent` was built against it. Loosening
the query would have hidden the gap and made the search worse.

## 11. Work in `/tmp` is not work

**What happened.** The validated capability existed only in `/tmp/grow`. Any cleanup
would have destroyed the only copy of something the demonstration needed.

**What changed.** The module now ships in `demos/capability/memory_recent/`, so the
demonstration is reproducible from a fresh clone. Anything worth keeping goes in the
repository; scratch space is scratch.

## 12. Honest output is a feature, not an apology

**Observed repeatedly.** `doctor` on an unconfigured home says the provider is
unreachable, that six protected files have no baseline, that every task will end
`blocked` until that is fixed, and exits 0 while saying so. A task whose criteria are
not met reports `partially verified` and names what is missing. A model claiming
completion does not close a task.

These are the outcomes the specification asks for, and they are the ones worth
protecting when someone later finds them inconvenient.

## 13. A test that cannot run is worse than a test that fails

**What happened.** Rewriting `tests/test_mission.py` clobbered one class body and
duplicated several others. `unittest` collected 16 of the 17 intended tests and
reported OK: the missing test simply did not exist any more, and the duplicates were
invisible because the later definition silently won.

**What changed.** `tests/test_hygiene.py` now reads the test sources as source and
asserts three things: no module defines the same class twice, every `TestCase`
subclass either has test methods or is inherited by one, and unittest discovery
collects exactly the number of test methods the sources contain. The mission's own
F test was found this way, after it had been silently absent from a passing suite.

## 14. A row with no owner is not evidence

**What happened.** Token usage from the model was recorded, but the `model_reply`
journal event was written without a task id, so the learning ledger could never
attribute a single call to the task that made it. The first ledger row in a real run
reported `prompt_tokens_actual: null` for a transport that had reported 900 tokens
per call.

**What changed.** The reply event carries the task id it belongs to, and the ledger
reads usage back from those rows. The fix was found by running a task end to end and
reading the row, not by reading the code.

## 15. Provenance is a claim the code has to keep true

**What happened.** The tool catalog reported every capability tool with
`source: "builtin"` and `validation: "builtin+tested"`. The data was never stored --
`register_external` kept the schema it needed for the model and dropped everything
else -- and the reader defaulted to the words that sound reassuring.

**What changed.** Provenance travels with the tool (`source: capability:word_count`,
`validation: capability:active`, and the permissions the module declares), and a test
fails if a capability-provided tool is ever described as a built-in again. A default
that flatters the system is a lie waiting for a reader.

## 16. A procedure is not an outcome

**What happened.** The mission's section 12 asks for demonstrations of a limited
retrieval, a discovered capability, a failure that changes the next action, a resume,
a reuse, and token accounting. An early version of the demonstration runner printed
those steps and reported `verified` for each -- including M2, where the capability
had been found but never executed, because the later session had been given no task
script at all. Five of six demonstrations "passed" while one of them proved nothing.

**What changed.** Every demonstration now asserts on observed state: the tool-call
rows in the journal, the ledger columns, the approval row status, the verification
outcome. A demonstration that cannot show its evidence records `not verified` and the
runner exits non-zero. The distinction between "I ran the procedure" and "the
procedure produced the result" has to be enforced by the code that reports it.

## 17. The boundary that is easier to enforce is the one to enforce

**What happened.** A rule refused every command argument containing shell
metacharacters. It looked like defence in depth and was in fact security theatre: no
shell is ever spawned, the interpreter is allowlisted by name, and the rule only
blocked legitimate Python one-liners and SQL. Meanwhile the real risk -- `sh -c` --
was a name that could simply be denied.

**What changed.** The rule is now: allowlisted program, no shell interpreters, no
control characters, arguments are passed literally. The surviving tests assert the
literal-passing behaviour rather than the old blanket refusal. A rule that cannot
point at the mechanism it protects is a rule that will be worked around.

## 18. A security probe that tampers with the checkout

**What happened.** The enforcement probes asserted "a modification of a protected file is
detected" by writing to `livium/guardian.py` in the live working tree and restoring it in a
`finally` block. The check that exists to prevent unauthorised modification was performing
one, on every run, and a crash between the write and the restore would have left the tree
tampered. It also depended on the operator having baselined their own home, so on a fresh
home it reported "tamper not detected" -- a real check reported as broken because the test
arrangement was wrong.

**What changed.** The probes run in their own home and tamper with a file they own, created
for the purpose. The one thing a probe may never do is the thing it is checking for.

## 19. A fingerprint over keywords is a fingerprint that loses records

**What happened.** Deferred observations were deduplicated by hashing the sorted set of
words four characters or longer. "one / observation behind one" and "two / observation
behind two" therefore had the *same* fingerprint, and the second observation was merged
into the first -- silently, because a merge is a normal and desirable event for a genuine
duplicate. The queue looked healthy and had lost a record.

**What changed.** Exact-duplicate detection hashes the whole normalised text; near
duplicates go through the token-overlap path, which now requires three shared significant
words before proposing a merge. A test captures two observations that share only their long
words and asserts they stay apart. The general rule: a hash used for identity must be taken
over the thing that must be identical, not over a summary that discards the differences.

## 20. The investigation path bypassed the permission engine

**What happened.** The first version of the follow-up runner executed the commands a
discovery mentioned by calling the subprocess runner directly. Every guard the rest of the
system depends on -- the allowlist, the approval path, the journal, the post-tool integrity
check -- lives in the *tool layer*, and the runner is underneath it. A path that reaches
underneath the guards is not a shortcut; it is a bypass, and it was reachable from a
command string that came out of an observation.

**What changed.** Follow-up executions go through `ToolRegistry.invoke`, so an executed
command is checked, journalled and followed by an integrity check exactly like any other.
The same lesson applies to reading: the runner now resolves paths through the workspace and
reports absence rather than assuming.

## 21. Verifying only at the end made verification useless for deciding anything

**What happened.** Acceptance criteria were executed after the loop had already decided the
task was over. The answer could still change the status, but it could not change the
*next step* -- so a plan to evaluate deferred work "after the task is verified" had no
moment at which that was true inside the loop.

**What changed.** Verification now runs when the model claims completion, its result is
cached for the task, and the same real checks still run at the end of every task. The model
still cannot close a task; the difference is that the harness's verdict is available to the
router while the run is still in progress.

## 22. A record that can be edited is not a record

**What happened.** The first design wrote integrity findings into the same journal as
everything else, where any code path could update them. The row a tamper attempt would most
like to rewrite is the row describing the tamper attempt.

**What changed.** Detections, approvals, refusals, recoveries and verification results go
into `integrity_event`, which refuses `UPDATE` and `DELETE` at the SQLite level. The
application does not merely avoid editing it; it is prevented from editing it. A test
attempts both and asserts they fail.

## 23. A recovery that is not verified is a claim

**What happened.** The tempting implementation of "restore a protected file" is to write
the baselined bytes and report success. The failure mode is a partial write, a
simultaneous modification, or an exception between the write and the report -- any of
which produces a system that says it recovered and did not.

**What changed.** `restore` writes atomically, re-hashes the file on disk, compares the
hash to the baselined digest, records `recovery_performed` or `recovery_failed`, and raises
if it did not verify. There is no code path that reports a recovery without checking the
bytes.

## 24. A fixture that lists tools falls behind the registry

**What happened.** The router's selection tests carried a hand-written list of tool names.
Two new tools were added and the tests kept passing -- against a tool set that no longer
existed, which made the meta-tool assertions vacuous.

**What changed.** `tests/test_hygiene.py` parses that fixture and compares it to the
registry's own declared names, so the list cannot go stale. It found a missing entry the
first time it ran.

## 25. Test code that is written but never runs

**What happened.** A demonstration and its test called the same capability by different
paths: the test asserted on a *fresh agent* whose script had not been supplied, so the run
under test made no tool calls at all and the assertion "the capability ran" failed for a
reason that had nothing to do with the capability.

**What changed.** The demo builds the script once and passes it to the agent that runs the
task. The general lesson, and the reason `test_hygiene.py` counts test methods at all: a
check must be shown to fail for the right reason at least once, or it is decoration.

## 26. A measurement that existed but was never attributed

**What happened.** The journal recorded every model reply with its provider usage, and the
ledger aggregates usage per task. The `model_reply` events were written without a
`task_id`, so the ledger's query — "replies belonging to this task" — could never find any.
The columns existed, the code that fills them existed, and the number was always NULL: a
measurement that was taken and then thrown away at the join.

**What changed.** `_call_model` now takes the task id and writes it with the event. The
lesson is about *where* to look for this class of bug: not at the code that computes a
number, but at the key that links it to the thing it describes.

## 27. `super().__init__()` is not optional in a subclass

**What happened.** A test client subclassed `ModelClient` and did not call
`super().__init__(config)`. Everything looked right until the first call, which failed on
a missing attribute that the base class sets. Inheriting the interface is not inheriting
the setup.

**What changed.** The client calls `super().__init__(config)` and the model-layer tests
assert behaviour through the parent's contract, including `stop_reason` — which the base
class requires and the test client had been omitting.

## 28. Two tests agreed with each other about the wrong number

**What happened.** A test asserted the ledger stored an estimate *and* a test asserted no
estimate was stored; both passed, because the two read different rows — one queried by
task id and one by insertion order. The disagreement was invisible until a third
assertion compared them.

**What changed.** The ledger assertions read the ledger row for the task under test, and a
later test asserts the two columns are *mutually exclusive* per task (actual XOR estimate).
A pair of tests that cannot disagree is worth more than a pair that happens to agree.

## 29. A result that reported "estimated" for a measurement that did not happen

**What happened.** The ledger has two token columns: measured usage and an estimate. The
estimate is computed by the context manager, summed over context reports. When a
provider reported no usage, the estimate column was populated — correct — but the
`usage_reported_calls` counter was derived from the same loop that skipped the missing
values, so a home with three tasks could show an estimate and zero reported calls. The
report's phrasing ("where usage was not reported the column is NULL") then described a
row that also carried an estimate, which reads as a contradiction unless both statements
are read carefully.

**What changed.** The report names both columns explicitly and the ledger test asserts the
XOR: exactly one of the two is present for every task. Precision about which number is
which is the whole point of keeping two columns.

## 30. Honest failures are cheaper than convenient successes

**What happened.** A demo step failed because the model's script produced a tool call the
permission engine refused. The tempting fix was to widen the allowlist so the demo would
pass. The refusal was correct: the demo had asked for the wrong thing.

**What changed.** The demo asks for what is permitted and shows the refusal path as its own
step. Three separate demos now *expect* a refusal and assert its reason. A demonstration
that only ever shows the happy path is a brochure.

## 31. A budget that is checked between items is a report, not a limit

**What happened.** The follow-up cycle enforced `max_model_calls` before starting each item
and totalled the spend from what the runner returned afterwards. A single investigation could
therefore make as many calls as it liked: the cycle only looked again once it was over. The
numbers were then assembled from three separate tallies -- the cycle's totals, the runner's
reported spend, and the ledger's own count -- and they disagreed in exactly the case that
mattered: an investigation the budget stopped had left the record `ready` while the ledger
counted it as investigated.

**What changed.** The counters moved onto `FollowUpBudget` itself, and the work charges them
before it happens: a model call, a step, a command execution, plus a deadline that refuses
work once the cycle's time is up. A refusal raises, the item goes back to `ready`, and the
limit's name goes on the record in one spelling. The ledger gained a column, because counting
an attempt and counting a conclusion in one number makes the ledger disagree with the record
it describes.

**Two things were learned from the tests, not from the code.** A limit of N allows N units
and refuses the N+1st, so a test for the stop has to supply one more turn than the limit --
the first version of the test failed because the script ran out of replies before the budget
ran out of room. And a demonstration that shares its workspace with earlier demonstrations is
not measuring its own budget: the first M10 run spent *their* queued items and reported 24
model calls under a two-call limit.

---

## Rules derived from these lessons

The agent's growth instructions (`livium/prompts/GROWTH.md`) encode the operational
form of the above, and `AGENTS.md` carries them into the workspace:

1. Measure a gap before claiming one; a tool returning nothing may be answering a
   different question correctly.
2. Reuse → compose → extend → build, in that order, with the reasoning written down.
3. Build the smallest thing that closes the gap, and delete parameters it cannot
   honour.
4. Test real behaviour against real storage, including the failure paths.
5. Never promote a skeleton, never promote a change whose tests fail, never promote
   without a named human.
6. Keep evidence: the journal, the test output, the file hash.
7. When the environment blocks a real verification, say so and name what would
   unblock it. Never substitute a demonstration of orchestration for a demonstration
   of the capability.
8. Assert on outcomes that only a working system can produce, and make the report of
   a demonstration fail when the evidence is missing.
9. Prefer the boundary that names its mechanism over the boundary that sounds strict.
10. Store provenance with the thing it describes; never let a reader default to a
    flattering label.
11. A check that tampers with the system it is checking is not a check; give it its own
    copy and its own state.
12. A hash that decides identity must be taken over the whole thing, not over a summary
    that discards the differences between two of them.
13. Verify at the moment the decision is made, not only at the end: a verification that
    cannot change the next step is a report, not a control.
14. When a number is wrong, check the join before checking the arithmetic: an
    unattributed measurement is a measurement that was never taken.
15. A fixture that lists part of the system is a claim about the system; make a test
    compare it to the real thing, or it will describe a version that no longer exists.
16. A tool that can reach underneath the guards is a bypass, however convenient the
    path: route effects through the layer that checks them.
17. Charge the limit where the spending happens, before it happens, and raise when it is
    reached. A limit checked after the fact is a description of the past.
18. Count an attempt and a conclusion separately. One number that covers both will
    eventually be read as the more flattering of the two.
19. When a stop is journalled, name it with the same sentence everywhere: two names for
    one limit read as two limits.
