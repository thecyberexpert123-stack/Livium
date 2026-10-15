# Mission demonstrations — observed results

Run against `/tmp/livium-05-mission-final` (a real LIVIUM_HOME). Model replies come from the scripted test double; everything else is the real runtime.

## M1 selective retrieval — verified

the store held 37 memory records; 2 reached the prompt and they are exactly the ones with the right terms

```json
{
  "records_in_store": 37,
  "records_injected": [
    34,
    35
  ],
  "relevant_ids": [
    34,
    35
  ],
  "all_injected_are_relevant": true,
  "prompt_chars": 10824,
  "tokens_estimated": 3093,
  "included_sections": {
    "instructions": 4,
    "objective": 2,
    "execution_state": 1,
    "memory": 3,
    "capabilities": 2,
    "background": 2
  },
  "tool_interfaces_exposed": [
    "read_file",
    "list_dir",
    "sha256",
    "write_file",
    "finish_task",
    "request_capability",
    "tool_info",
    "record_discovery"
  ],
  "tools_available": 17,
  "route": "execute",
  "model_replies": "scripted test double (stated in every reply record)"
}
```

## M2 capability discovery — verified

the capability was found by requirement words, its declared interface was loaded, and it ran 1 time(s) in a later session

```json
{
  "lifecycle": {
    "status": "active",
    "tools": [
      "word_count"
    ],
    "test_exit_code": 0,
    "test_duration_ms": 123,
    "network_isolated": null
  },
  "search_matches": [
    {
      "name": "word_count",
      "status": "active",
      "validated": true,
      "matched_terms": [
        "count",
        "file",
        "the",
        "words"
      ]
    }
  ],
  "interface_tool_schema": [
    {
      "name": "word_count",
      "description": "Count words in a workspace file.",
      "parameters": {
        "type": "object",
        "properties": {
          "path": {
            "type": "string"
          }
        },
        "required": [
          "path"
        ]
      },
      "side_effects": false
    }
  ],
  "interface_limitations": "ASCII whitespace only; no per-language tokenisation.",
  "catalog_entry": {
    "name": "word_count",
    "purpose": "Count words in a workspace file.",
    "permissions": [
      "read"
    ],
    "side_effects": false,
    "source": "capability:word_count",
    "validation": "capability:active"
  },
  "capability_tool_calls": [
    {
      "arguments_digest": "9b83205689173614",
      "category": "",
      "denied": false,
      "error": "",
      "ok": true,
      "purpose": "the discovered capability",
      "step": 2,
      "tool": "word_count"
    }
  ],
  "task_status": "complete",
  "verification": "verified",
  "route_decisions": [
    [
      1,
      "execute"
    ],
    [
      2,
      "execute"
    ],
    [
      3,
      "execute"
    ],
    [
      4,
      "verify"
    ]
  ]
}
```

## M3 failure changes the next action — verified

the write was refused as requiring approval, the run stopped instead of retrying, and one human decision released exactly one execution

```json
{
  "refusal": {
    "status": "awaiting_approval",
    "denials": 1,
    "file_exists": false,
    "approvals_requested": 1,
    "route_decisions": [
      [
        1,
        "execute"
      ],
      [
        2,
        "request_authorization"
      ]
    ],
    "pending": [
      {
        "id": 1,
        "task_id": "m3-approval",
        "tool": "write_file",
        "arguments": "{\"content\":\"v1\\n\",\"path\":\"/tmp/livium-05-mission-final/restricted/release.md\",\"purpose\":\"the release file\"}",
        "reason": "write needs approval: /tmp/livium-05-mission-final/restricted/release.md is inside an approval root (/tmp/livium-05-mission-final/restricted). A human decides, not the agent; see `livium approvals`.",
        "status": "pending",
        "created_at": "2026-10-15T16:48:07+00:00",
        "decided_at": null,
        "decided_by": null,
        "note": null
      }
    ]
  },
  "human_decision": {
    "decided": true,
    "id": 1,
    "status": "approved",
    "by": "Arena Test Operator",
    "recorded": true,
    "after_approval": {
      "status": "partial",
      "denials": 0,
      "file_exists": true,
      "route_decisions": [
        [
          1,
          "execute"
        ],
        [
          2,
          "execute"
        ],
        [
          3,
          "retrieve_context"
        ]
      ]
    },
    "second_attempt_same_task": {
      "status": "awaiting_approval",
      "denials": 1,
      "approvals_requested": 1
    },
    "approval_rows": [
      {
        "id": 2,
        "status": "pending",
        "decided_by": null
      },
      {
        "id": 1,
        "status": "consumed",
        "decided_by": "Arena Test Operator"
      }
    ]
  }
}
```

## M4 resume after interruption — verified

a killed process left one unconfirmed side effect; the resumed run established its real outcome from the filesystem before continuing

```json
{
  "child_output": "intent 3 recorded and the file landed; killing the process before the intent could be confirmed",
  "child_exit": "killed by signal 9",
  "pending_intents_before_resume": 1,
  "inspection_next_action": "Every interrupted operation has been accounted for from evidence; the task can continue from: its objective",
  "inspection_findings": [
    {
      "intent_id": 3,
      "tool": "write_file",
      "determination": "confirmed_complete",
      "detail": "/tmp/livium-05-mission-final/workspace/interrupted.md matches the intended content exactly (sha256 9d9a4fd0227729dc\u2026), so the write completed even though the state record was not updated; do not repeat it",
      "evidence": "sha256=9d9a4fd0227729dc5b15a7f389550f3fb5e69d8ee82561e48831966323bc720e size=13 mtime=1792082888298689821 path=/tmp/livium-05-mission-final/workspace/interrupted.md"
    }
  ],
  "intent_rows_after_resume": [
    {
      "tool": "write_file",
      "status": "done"
    }
  ],
  "resumed_task_status": "partial",
  "resumed_verification": "verified",
  "route_decisions": [
    [
      1,
      "execute"
    ],
    [
      2,
      "retrieve_context"
    ]
  ]
}
```

## M5 experience reuse across processes — verified

a record written by one process was retrieved by another and the tool it named was the tool that ran; the ledger stores the link, not an impression

```json
{
  "written_by_process_1": {
    "memory_id": 49,
    "action": "created"
  },
  "process_2": {
    "status": "complete",
    "verification": "verified",
    "capability_reused": true,
    "new_development": false,
    "experience_influenced": true,
    "memory_ids": [
      49,
      39,
      38
    ],
    "experience_evidence": {
      "reuse": [
        {
          "memory_id": 49,
          "kind": "capability",
          "key": "word_count",
          "tools": [
            "word_count"
          ],
          "why": "the record named a tool that this run then executed"
        }
      ],
      "avoided_repetition": []
    },
    "tools_executed": [
      "write_file",
      "word_count",
      "finish_task",
      "write_file",
      "write_file",
      "write_file",
      "write_file",
      "word_count",
      "finish_task"
    ],
    "route_decisions": [
      [
        1,
        "execute"
      ],
      [
        2,
        "execute"
      ],
      [
        3,
        "execute"
      ],
      [
        4,
        "verify"
      ]
    ]
  }
}
```

## M6 token accounting — verified

a reporting transport produced actual counts; a silent one produced nulls and a labelled estimate, with no number invented for the missing measurement

```json
{
  "rows": {
    "reported": {
      "model_calls": 2,
      "prompt_tokens_actual": 2900,
      "completion_tokens_actual": 180,
      "usage_reported_calls": 2,
      "tokens_estimated": 10391,
      "task_status": "complete"
    },
    "unreported": {
      "model_calls": 2,
      "prompt_tokens_actual": null,
      "completion_tokens_actual": null,
      "usage_reported_calls": 0,
      "tokens_estimated": 10609,
      "task_status": "complete"
    }
  },
  "model": "usage-reporting test double (no real provider is reachable here)"
}
```

## M7 deferred discovery — verified

the task finished complete (verified) through ['execute', 'execute', 'execute', 'verify', 'evaluate_discoveries']; one observation was captured while it ran and the queue was evaluated after it was verified; the follow-up ran as its own task and closed completed with the harness's evidence attached

```json
{
  "task_status": "complete",
  "verification": {
    "total": 1,
    "passed": 1,
    "counts": {
      "passed": 1
    },
    "state": "verified",
    "remaining": []
  },
  "route_decisions": [
    "execute",
    "execute",
    "execute",
    "verify",
    "evaluate_discoveries"
  ],
  "discoveries": [
    {
      "id": 1,
      "status": "completed",
      "title": "inventory.txt does not say what its numbers mean",
      "provenance": "observed_content",
      "flagged": "[\"silently\"]",
      "depth": 0,
      "spawned_by": null,
      "outcome": "investigated as task a9b610a1a1e2 and the harness verified the investigation touched real state. The conclusion below is the task's own claim, not a harness finding: the file names no unit anywhere, so the ambiguity is real: 'bolts: 12' could be a count or a measure",
      "verification_evidence": "task a9b610a1a1e2: tools executed read_file, finish_task; verification verified; tools/verification: complete; checks: event 'tool_call' recorded at 2026-10-15T16:48:10+00:00"
    }
  ],
  "ledger": {
    "discoveries_captured": 1,
    "followups_investigated": 1,
    "model_calls": 3,
    "tools_executed": 3,
    "verification_state": "verified"
  },
  "discovery_events": [
    "discovery_cycle_complete",
    "discovery_transitioned",
    "discovery_transitioned",
    "discovery_captured"
  ],
  "model_note": "replies from the scripted test double; the queue, the follow-up task, its verification and the ledger are real code paths"
}
```

## M8 queue workflow — verified

`discovery list` returned 2 record(s) and `discovery review` reported 1 open item(s); a legal transition was accepted and an illegal one was refused with: a discovery cannot be completed without verification evidence

```json
{
  "list_exit": 0,
  "review_exit": 0,
  "records": [
    {
      "id": 2,
      "status": "ready",
      "title": "the demonstration needs something in the queue",
      "confidence": 0.4,
      "risk": "unknown",
      "depth": 0,
      "provenance": "agent_observation",
      "flagged": [],
      "source_task": "demo-m8",
      "spawned_by": null
    },
    {
      "id": 1,
      "status": "completed",
      "title": "inventory.txt does not say what its numbers mean",
      "confidence": 0.7,
      "risk": "low",
      "depth": 0,
      "provenance": "observed_content",
      "flagged": [
        "silently"
      ],
      "source_task": "59b94274ee35",
      "spawned_by": null
    }
  ],
  "counts": {
    "pending": 0,
    "ready": 1,
    "investigating": 0,
    "blocked": 0,
    "completed": 1,
    "rejected": 0,
    "postponed": 0,
    "open": 1
  },
  "legal_transition_exit": 0,
  "illegal_transition_exit": 2,
  "illegal_transition_message": "a discovery cannot be completed without verification evidence",
  "note": "the CLI runs in a separate process against the same durable database"
}
```

## M9 integrity response — verified

the modification became 1 append-only record(s) and `integrity status` reported tampered with 1 changed file(s); the structural review returned 1 finding(s); an unattributable recovery was refused with: refused: a recovery needs a named human; the named recovery restored the file and the digest on disk matches (True)

```json
{
  "baseline": {
    "baseline_files": 13,
    "approved_by": "demonstration setup",
    "forced": true,
    "replaced": 12
  },
  "status_after": {
    "status": "tampered",
    "changed": [
      "/tmp/livium-05-mission-final/rules.py"
    ],
    "exit": 1
  },
  "recorded_events": [
    {
      "id": 4,
      "ts": "2026-10-15T16:48:11+00:00",
      "event": "change_detected",
      "tier": null,
      "paths": [
        "/tmp/livium-05-mission-final/rules.py"
      ],
      "observed": {
        "digests": {
          "/tmp/livium-05-mission-final/rules.py": "a6ec5b1ff4babf837ab72547b47de7092dc7272dd37c2bf8d9f0692d7e69ed43"
        }
      },
      "expected": {
        "digests": {
          "/tmp/livium-05-mission-final/rules.py": "6ba0d22d9f7a2466ed1784567ef7a5662bdbe900ce42ad4d3a8f1511ebc755cf"
        }
      },
      "detection": "livium integrity status",
      "policy": {
        "action": "a protected component differs from its baseline; no change may be promoted while this stands"
      },
      "decision": "detected",
      "authorization": null,
      "evidence": {
        "tiers": [
          "operational"
        ]
      },
      "recovery": null,
      "outcome": "unauthorised_until_reviewed",
      "uncertainty": "the change is unapproved; who made it is only claimed when the journal supports it",
      "task_id": null,
      "attributable_to": null,
      "tool": null,
      "revision": {
        "head": "ba2e5f22df83c2e2a708259b22af6a1a3db8a0e1",
        "working_tree_status": "",
        "last_commit_touching_file": "",
        "note": "git state only; the working tree is not verified by git"
      }
    }
  ],
  "review": [
    {
      "path": "/tmp/livium-05-mission-final/rules.py",
      "tier": "operational",
      "decision": "requires_review",
      "why_protected": "the configuration protects this path, but the policy does not name it and so cannot yet say what a legitimate change would look like",
      "permits": "nothing by default: a change is refused, recorded and left to a human",
      "authorizer": "a named human, by re-baselining after review",
      "verification": "review of the difference, then a re-baseline of this file only",
      "requires": [
        "a named human approver"
      ],
      "weakening_findings": [],
      "findings": [],
      "proposed_by": "unknown",
      "reason": "",
      "note": "this reviews the change; it does not authorise it and does not apply it"
    }
  ],
  "anonymous_recovery_refused": {
    "exit": 4,
    "message": "refused: a recovery needs a named human"
  },
  "named_recovery": {
    "exit": 0,
    "payload": {
      "restored": true,
      "path": "/tmp/livium-05-mission-final/rules.py",
      "digest": "6ba0d22d9f7a2466ed1784567ef7a5662bdbe900ce42ad4d3a8f1511ebc755cf",
      "previous": "a6ec5b1ff4babf837ab72547b47de7092dc7272dd37c2bf8d9f0692d7e69ed43",
      "approved_by": "Arena Test Operator"
    },
    "digest_on_disk_after": "6ba0d22d9f7a2466ed1784567ef7a5662bdbe900ce42ad4d3a8f1511ebc755cf",
    "digest_matches": true
  }
}
```

## M10 follow-up budget — verified

the task that noticed the observation finished complete (verified) and paid nothing; the follow-up that did pay made exactly 2 model call(s) of the 2 allowed, was stopped by the budget (model_calls budget exhausted (2 of 2)), and its record stayed ready for a later cycle; the ledger counts 0 concluded and 1 stopped

```json
{
  "task_status": "complete",
  "task_verification": {
    "total": 1,
    "passed": 1,
    "counts": {
      "passed": 1
    },
    "state": "verified",
    "remaining": []
  },
  "investigation_model_replies": 2,
  "cycle": {
    "budget": {
      "exhausted": "model_calls budget exhausted",
      "limits": {
        "max_items": 2,
        "max_model_calls": 2,
        "max_seconds": 120.0,
        "max_steps": 8,
        "max_tokens": 20000
      },
      "spent": {
        "model_calls": 2,
        "steps": 3,
        "tokens": 0
      },
      "tokens_measured": false
    },
    "investigated": [
      1
    ],
    "spent": {
      "model_calls": 2,
      "seconds": 0.025,
      "steps": 3,
      "tokens": 0
    },
    "stopped_because": "model_calls budget exhausted (2 of 2)",
    "tokens_measured": false
  },
  "stop_events": [
    {
      "kind": "model_calls",
      "limit": 2.0,
      "reason": "model_calls budget exhausted (2 of 2)",
      "spent": 2.0,
      "step": 3
    }
  ],
  "discovery_records": [
    {
      "id": 1,
      "status": "ready"
    }
  ],
  "ledger": {
    "followups_investigated": 0,
    "followups_stopped": 1
  },
  "model_note": "replies from the scripted test double; the budget, the loop, the journal, the ledger and the queue are real code paths"
}
```
