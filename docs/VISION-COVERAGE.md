# Project vision coverage map

This map connects the project vision to the implementation and its evidence. A test
reference means the repository contains an assertion for that behavior; it does not
mean this audit re-ran the test or that every code path is covered.

| Vision requirement | Implementation | Evidence in repository | Status / remaining work |
|---|---|---|---|
| Real task loop, tool use, observation, outcome verification | `agent.py`, `tools.py`, `verification.py` | `test_agent.py`, `test_tools.py`, `test_verification.py`; demo A | Implemented; successful live-provider operation still needs to be observed in the target environment. |
| Persistent, provenance-aware memory and experience reuse | `memory.py`, `store.py`, `ledger.py` | `test_memory.py`, `test_ledger.py`; demo B/E | Implemented; retrieval is keyword based, and there is no sustained real-task learning benchmark. |
| Per-step context selection and resource constraints | `context.py`, `models.py` | `test_context.py`, `test_models.py`; demo C | Implemented; token counts are estimates unless a provider reports actual usage. |
| Task-directed routing, failure investigation, and capability reuse | `router.py`, `capabilities.py` | `test_router.py`, `test_mission.py` | Implemented for enumerated states; broad model judgment is not proven by deterministic tests. |
| Inspect interrupted effects and recover without blind replay | `recovery.py`, `store.py` | `test_recovery.py`, `test_agent.py`; demo D | Implemented for observable intent/artifact cases; unobservable effects remain explicitly unknown. |
| Capture discoveries with provenance, then defer and budget follow-up | `discovery.py`, `router.py`, `ledger.py` | `test_discovery.py`, `test_mission.py`; mission demos M7–M10 | Implemented; duplicate detection/ranking are lexical heuristics and token overrun is known to be post-call detectable only. |
| Develop reusable capabilities while preventing unreviewed activation | `capabilities.py`, `permissions.py` | `test_capabilities.py`, Studio lifecycle tests; demo E/F | Implemented with named-human promotion; generated Python is not sandboxed and approver identity is not authenticated. |
| Growth Guardian protects governance and verifies changes | `guardian.py`, `integrity.py`, `config.py` | `test_guardian.py`, `test_integrity.py`; M9 | Implemented as tamper detection, records, probes, human review, and verified restore; same-UID code can bypass in-process controls. Probes are finite, not a proof of all possible non-weakening changes. |
| Workspace separates experiments from promoted implementation | Capability lab and per-home promoted manifests; Studio workspace APIs | `test_capabilities.py`, `ui/test/*` | Implemented at application level; OS-level filesystem confinement is absent, so arbitrary subprocess/Python code has the user's privileges. |
| Honest distinction between claim, verification, estimate, and real/test-double output | `models.py`, `verification.py`, `report.py`, Studio transcript | `test_agent.py`, `test_models.py`, `test_ledger.py`, `ui/src/App.test.jsx` | Implemented for recorded fields and criteria; task quality depends on meaningful acceptance criteria. |
| Reliable completion of increasingly demanding real tasks and measured improvement | Ledger, reports, mission demos | `test_ledger.py`, `test_mission.py`; scripted mission demonstrations | **Not yet demonstrated.** The recorded demos are mostly test-double-driven and are not a longitudinal live-model benchmark. Run and retain real-provider evidence before claiming improvement. |

## What “full coverage” means here

The codebase has feature and behavior tests, not a published line/branch coverage
percentage. Existing test counts are not proof that every behavior or adversarial
case is covered. Before calling the vision fully covered, the remaining evidence should
include at least:

1. A real provider end-to-end run in the intended Kali WSL environment (the smoke
   command in `WSL-KALI-SETUP.md` records one).
2. A repeatable, multi-task evaluation with acceptance criteria and recorded actual
   provider usage, task outcomes, failures, recovery, and memory reuse across process
   restarts.
3. Independent OS-level isolation and authenticated approval if the threat model
   includes malicious generated or promoted code. The current in-process controls do
   not supply either guarantee.
4. A measured statement/branch coverage report and explicit untested-path review if
   “full coverage” means code coverage.

Do not report these items as complete until their evidence exists. The exact trust and
measurement limits are described in `docs/LIMITATIONS.md` §§1–7, 15–21.
