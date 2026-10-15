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
