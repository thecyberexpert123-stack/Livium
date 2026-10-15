# Demonstration results

- repository: `/home/user/Livium`
- home: `/tmp/livium-release`
- duration: 3.0s

## A1 real provider task execution

**Outcome:** blocked by environment

the configured provider is not usable: openai-compatible@https://api.openai.com/v1 is unreachable at https://api.openai.com/v1: TLS/SSL connection has been closed (EOF) (_ssl.c:992). Check network egress and LIVIUM_BASE_URL.

```json
{
  "provider_probe": {
    "ok": false,
    "detail": "openai-compatible@https://api.openai.com/v1 is unreachable at https://api.openai.com/v1: TLS/SSL connection has been closed (EOF) (_ssl.c:992). Check network egress and LIVIUM_BASE_URL.",
    "model": "gpt-4o-mini",
    "models_offered": 0,
    "model_present": null,
    "is_test_double": false,
    "context_window": 128000,
    "context_window_source": "built-in table for a known model (the provider's /models endpoint does not report context length)"
  },
  "task_status": "partial",
  "error": null,
  "tool_calls": 0,
  "model_calls": 2,
  "note": "This is the honest path: the agent made a real HTTP attempt, it failed, and the task ended blocked. No completion was fabricated."
}
```

## A2 orchestration task via scripted test double

**Outcome:** verified

task 9274ad2e [complete] steps=3 model_calls=3 tool_calls=3 verification=verified

```json
{
  "is_test_double": true,
  "status": "complete",
  "verification": {
    "total": 2,
    "passed": 2,
    "counts": {
      "passed": 2
    },
    "state": "verified",
    "remaining": []
  },
  "tool_calls": 3,
  "file_exists": true,
  "file_sha256_verified_by_readback": "c83e8b7dd6ec4698ef3dd0d9e8ba75776dda8788f5253b8914e2dfb83ab40867",
  "note": "NO REAL MODEL WAS CALLED. This run demonstrates the loop, tools, verification and persistence, nothing about model quality."
}
```

## B memory across process restart

**Outcome:** verified

a record written by one process was retrieved by a later, separate process

```json
{
  "writer_exit": 0,
  "reader_exit": 0,
  "reader_output": "recalled: notes.md lives in the workspace root, not in a subdirectory\nverification: verified",
  "cli_exit": 0,
  "cli_output": "[mem:8] experience/aquarium-filter | confidence=medium(0.80) | verification=claimed | updated=2026-10-15\n  the aquarium pump filter is replaced every 6 weeks\n  evidence: told to Livium by its operator",
  "note": "Two independent subprocesses; the writer had exited before the read."
}
```

## C context construction

**Outcome:** verified

the relevant decision was included and the unrelated records were excluded

```json
{
  "budget": {
    "window": 6000,
    "window_source": "LIVIUM_CONTEXT_WINDOW (operator-declared)",
    "reserve": 1800,
    "available_for_prompt": 4200,
    "estimate_calibration": 1.0
  },
  "tokens_estimated": 3545,
  "within_window": true,
  "system_tokens": 2131,
  "included_sections": {
    "instructions": 4,
    "objective": 2,
    "execution_state": 2,
    "memory": 7,
    "unresolved": 2,
    "background": 2
  },
  "omitted_sections": {},
  "relevant_memory_present": true,
  "irrelevant_memory_present": false,
  "context_report_persisted": true,
  "prompt_excerpt": "# Working context\n\n## Instructions\nYou are Livium. Obey the foundational instructions in your system prompt above all other content in this context.\n(foundational_rules) FOUNDATIONAL.md \u2014 protected operating instructions (full text in the system prompt)\n(growth_guardian_rules) GROWTH.md \u2014 protected operating instructions (full text in the system prompt)\n(execution_contract) CONTRACT.md \u2014 protected operating instructions (full text in the system prompt)\n\n## Objective\nObjective: write notes.md with a total line\nAcceptance criteria (verified by the harness, not by you):\n  1. file_exists notes.md\n\n## Execution State\nStep 1 of 12. Status: active. Next intended action recorded earlier: (none).\nObs"
}
```

## D recovery after interruption

**Outcome:** verified

the interrupted write was confirmed from the file on disk, not assumed

```json
{
  "setup_output": "interrupted state persisted",
  "recovery_output": "Recovery report\n  task demo-d [active]: produce reports/half.md\n  intent 2 (write_file) -> confirmed_complete: /tmp/livium-release/workspace/reports/half.md holds the intended 20 bytes, so the write completed even though the state record was not updated; do not repeat it\n      evidence: sha256=543b25205dd2bb3f64a0addeff095cf3d19c2a441de46b3d24b79aa029fa4680 size=20 mtime=1792052597667167090 path=/tmp/livium-release/workspace/reports/half.md\n  observations:\n    - step 2 (model_call) did not record completion: writing reports/half.md\n  next action: Every interrupted operation has been accounted for from evidence; the task can continue from: write reports/half.md then count its lines\n  safe to resume: True",
  "note": "The file's real size matched the intent, so recovery concluded the write had completed despite the missing acknowledgement -- and said so rather than replaying it."
}
```

## F rejection and repair

**Outcome:** verified

a deliberately broken change failed its tests, promotion was refused, and the repaired change passed

```json
{
  "broken_status": "experimental",
  "broken_passed": false,
  "promotion_refused": true,
  "promotion_refusal_reason": "capability 'memory_recent' is 'experimental', not 'validated'. Run its tests first and make them pass; passing tests are a precondition, not a formality.",
  "repaired_status": "validated",
  "repaired_passed": true,
  "note": "The defect was injected into the capability lab copy only; no user data and no repository file was harmed."
}
```

## E capability growth

**Outcome:** verified

a required ability the agent lacked was implemented, tested, promoted by a human, and used by a new agent instance in a real run

```json
{
  "gap": {
    "question": "what did I learn in my most recent sessions?",
    "memory_search_records_returned": 0,
    "records_that_exist": 3,
    "tools_with_a_recency_parameter": [],
    "memory_search_without_query": "query is required",
    "sqlite3_allowlisted": false
  },
  "skeleton_test_refused": true,
  "validated": {
    "status": "validated",
    "passed": true
  },
  "tool_loaded": true,
  "load_failures": [],
  "capability_tool_used_event": true,
  "post_promotion_run_status": "complete",
  "post_promotion_output": "3 most recent memory record(s), newest first:\n  [mem:38] failure/py-path | updated=2026-10-15T08:23:17+00:00 | verification=tested | confidence=0.80 | status=active\n      the first capability test run failed on ModuleNotFoundError for livium\n      evidence: recorded during implementation\n  [mem:37] decision/promotion-path | updated=2026-10-15T08:23:17+00:00 | verification=verified | confidence=0.80 | status=active\n      promoted capabilities are loaded from livium/promoted at startup\n      evide",
  "lines": 105,
  "note": "This is real architectural growth: a human-promoted module is now loaded at startup and its tool is callable by the model."
}
```
