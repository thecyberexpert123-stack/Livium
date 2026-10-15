"""Operations held for human approval (mission sections 4 and 10).

The property under test is not "the queue works". It is: *the model cannot release
itself*, one decision covers exactly one execution, and a refusal is recorded as a
refusal rather than being quietly retried. Those three things are checked against
the real filesystem, not against a returned string.
"""

from __future__ import annotations

import json
import unittest

from support import LiviumTestCase

from livium.approvals import ApprovalQueue
from livium.permissions import APPROVAL, PermissionEngine
from livium.store import Store
from livium.tools import ToolContext, ToolRegistry

APPROVAL_SCRIPT = [
    {"tool_calls": [{"name": "write_file", "arguments": {
        "path": "@@RESTRICTED@@/release.md", "content": "v1\n",
        "purpose": "the deliverable"}}]},
    {"tool_calls": [{"name": "finish_task", "arguments": {
        "summary": "wrote the release file", "status": "complete"}}]},
]


class ApprovalFixture(LiviumTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.restricted = self.home.dir / "restricted"
        self.restricted.mkdir(parents=True, exist_ok=True)

    def make_agent(self, script=None):
        """A fresh agent on the same home, with the restricted root held for approval."""

        resolved = []
        for entry in (script or APPROVAL_SCRIPT):
            calls = []
            for call in entry["tool_calls"]:
                arguments = {}
                for key, value in call["arguments"].items():
                    arguments[key] = str(value).replace("@@RESTRICTED@@", str(self.restricted))
                calls.append({"name": call["name"], "arguments": arguments})
            resolved.append({"tool_calls": calls})
        agent = self.agent(script=resolved,
                           LIVIUM_APPROVAL_WRITE_ROOTS=str(self.restricted))
        agent.start()
        agent.guardian.record_baseline(approved_by="Test Human", force=True)
        return agent

    def queue(self, config=None) -> ApprovalQueue:
        return ApprovalQueue(self.store(config or self.config()))


class RefusalTests(ApprovalFixture):
    def test_a_write_inside_an_approval_root_is_held_not_allowed(self) -> None:
        engine = PermissionEngine(self.config(
            LIVIUM_APPROVAL_WRITE_ROOTS=str(self.restricted)))
        decision = engine.check_write(self.restricted / "release.md")
        self.assertEqual(decision.outcome, APPROVAL)
        self.assertIn("approval", decision.reason.lower())

    def test_outside_every_root_it_is_a_plain_denial(self) -> None:
        engine = PermissionEngine(self.config())
        decision = engine.check_write("/etc/passwd")
        self.assertEqual(decision.outcome, "deny")

    def test_the_tool_layer_reports_approval_required_and_writes_nothing(self) -> None:
        agent = self.make_agent()
        run = agent.run_task("write the release file into the restricted directory")
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.approvals_requested, 1)
        self.assertFalse((self.restricted / "release.md").exists(),
                         "a held operation must not have happened")
        self.assertTrue(run.error and "approve" in run.error)

    def test_the_request_is_recorded_with_the_exact_arguments(self) -> None:
        agent = self.make_agent()
        run = agent.run_task("write the release file into the restricted directory")
        pending = ApprovalQueue(agent.store).pending()
        self.assertEqual(len(pending), 1)
        arguments = json.loads(pending[0].arguments)
        self.assertEqual(arguments["path"], str(self.restricted / "release.md"))
        self.assertEqual(pending[0].tool, "write_file")
        self.assertEqual(pending[0].status, "pending")
        self.assertIsNone(pending[0].decided_by)

    def test_the_refusal_is_a_recorded_boundary_violation(self) -> None:
        agent = self.make_agent()
        run = agent.run_task("write the release file into the restricted directory")
        events = [json.loads(r["payload"]) for r in
                  agent.store.query("SELECT payload FROM event WHERE type='boundary_violation'")]
        outcomes = [e.get("outcome") for e in events]
        self.assertIn("approval_required", outcomes)
        self.assertEqual(run.denials, 1)


class DecisionTests(ApprovalFixture):
    def test_an_anonymous_decision_is_refused(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory")
        queue = ApprovalQueue(agent.store)
        request = queue.pending()[0]
        # The list stops the words that describe nobody. It is not, and cannot be, a
        # proof of identity: what makes a decision attributable is that it can only be
        # made at a terminal, by someone who typed a name, and that the name is recorded
        # and shown wherever the decision is reported.
        for attribution in ("", "   ", "Anonymous", "unknown", "-", "N/A"):
            with self.subTest(by=attribution):
                with self.assertRaises(ValueError):
                    queue.decide(request.id, approved=True, by=attribution)
        self.assertEqual(queue.get(request.id).status, "pending",
                         "a refused decision must leave the request pending")

    def test_a_decision_records_who_made_it(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory")
        queue = ApprovalQueue(agent.store)
        request = queue.pending()[0]
        decided = queue.decide(request.id, approved=True, by="Aditya", note="expected release")
        self.assertEqual(decided.status, "approved")
        self.assertEqual(decided.decided_by, "Aditya")
        self.assertIsNotNone(decided.decided_at)

    def test_a_request_cannot_be_decided_twice(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory")
        queue = ApprovalQueue(agent.store)
        request = queue.pending()[0]
        queue.decide(request.id, approved=True, by="Aditya")
        with self.assertRaises(ValueError):
            queue.decide(request.id, approved=False, by="Someone Else")

    def test_an_approval_is_idempotent_while_it_is_still_pending(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory")
        queue = ApprovalQueue(agent.store)
        request = queue.pending()[0]
        again = queue.request(task_id=request.task_id, tool=request.tool,
                              arguments=json.loads(request.arguments), reason="same thing")
        self.assertEqual(again.id, request.id,
                         "the same operation must not create a second request")
        self.assertEqual(len(queue.pending()), 1)

    def test_no_registered_tool_can_approve_anything(self) -> None:
        agent = self.make_agent()
        names = agent.tools.names()
        suspicious = [n for n in names if "approv" in n or "decide" in n or "consent" in n]
        self.assertEqual(suspicious, [],
                         "an approval route exposed as a tool is an approval the model can grant")
        result = agent.tools.invoke("approvals", {"id": 1, "approve": True},
                                    ToolContext(task_id="t", step=1))
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown_tool")


class EnforcementTests(ApprovalFixture):
    def test_the_approved_operation_runs_once_and_the_authorisation_is_consumed(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory",
                       task_id="approval-task")
        queue = ApprovalQueue(agent.store)
        request = queue.pending()[0]
        queue.decide(request.id, approved=True, by="Aditya")

        second = self.make_agent()
        run = second.run_task("write the release file into the restricted directory",
                              task_id="approval-task", resume=True)
        self.assertEqual(run.denials, 0, "the human approved exactly this operation")
        self.assertTrue((self.restricted / "release.md").exists())
        self.assertEqual(ApprovalQueue(second.store).get(request.id).status, "consumed")

    def test_the_same_authorisation_cannot_cover_a_second_execution(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory",
                       task_id="approval-task")
        queue = ApprovalQueue(agent.store)
        queue.decide(queue.pending()[0].id, approved=True, by="Aditya")

        second = self.make_agent()
        second.run_task("write the release file into the restricted directory",
                        task_id="approval-task", resume=True)
        third = self.make_agent()
        run = third.run_task("write the release file into the restricted directory",
                             task_id="approval-task", resume=True)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertEqual(run.approvals_requested, 1,
                         "a second execution needs a fresh human decision")

    def test_an_approval_for_one_task_does_not_authorise_another(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory", task_id="task-a")
        queue = ApprovalQueue(agent.store)
        queue.decide(queue.pending()[0].id, approved=True, by="Aditya")

        other = self.make_agent()
        run = other.run_task("write the release file into the restricted directory",
                             task_id="task-b")
        self.assertEqual(run.status, "awaiting_approval")
        self.assertFalse((self.restricted / "release.md").exists())

    def test_a_rejection_is_not_re_requested_with_identical_arguments(self) -> None:
        agent = self.make_agent()
        agent.run_task("write the release file into the restricted directory",
                       task_id="approval-task")
        queue = ApprovalQueue(agent.store)
        queue.decide(queue.pending()[0].id, approved=False, by="Aditya", note="not this file")

        second = self.make_agent()
        run = second.run_task("write the release file into the restricted directory",
                              task_id="approval-task", resume=True)
        self.assertFalse((self.restricted / "release.md").exists())
        self.assertEqual(len(ApprovalQueue(second.store).pending()), 0,
                         "a standing rejection must not be re-asked unchanged")


class EndToEndTests(ApprovalFixture):
    """The whole loop: refused, decided by a person, executed once, verified.

    This is the flow the CLI offers a human, reproduced here so that the deadlock it
    used to have cannot come back: an approved write used to be impossible to verify,
    because the criterion that inspects the written file was refused for being outside
    the read roots, and the task closed 'not verified' for a reason that had nothing to
    do with the work.
    """

    def test_an_approved_write_is_performed_once_and_then_verified(self) -> None:
        objective = "write the release file into the restricted directory"
        target = self.restricted / "release.md"
        criteria = [{"kind": "file_exists", "path": str(target)}]

        first = self.make_agent()
        run = first.run_task(objective, acceptance=criteria)
        self.assertEqual(run.status, "awaiting_approval")
        self.assertFalse(target.exists(), "nothing may be written before the decision")
        request = self.queue().pending()[0]

        with self.assertRaises(ValueError):
            self.queue().decide(request.id, approved=True, by="Anonymous")
        decided = self.queue().decide(request.id, approved=True, by="Aditya (human)",
                                      note="release")
        self.assertEqual(decided.status, "approved")
        self.assertEqual(decided.decided_by, "Aditya (human)")

        second = self.make_agent()
        resumed = second.run_task(objective, acceptance=criteria, task_id=run.task_id,
                                  resume=True)
        self.assertEqual(resumed.status, "complete")
        self.assertEqual(resumed.verification["state"], "verified",
                         json.dumps(resumed.criteria_results))
        self.assertEqual(resumed.denials, 0, "the authorised call must not be refused again")
        self.assertTrue(target.exists())
        self.assertEqual(target.read_text(), "v1\n")
        self.assertEqual(self.queue().get(request.id).status, "consumed")

    def test_a_second_execution_needs_a_second_decision(self) -> None:
        objective = "write the release file into the restricted directory"
        target = self.restricted / "release.md"
        criteria = [{"kind": "file_exists", "path": str(target)}]

        run = self.make_agent().run_task(objective, acceptance=criteria)
        first_request = self.queue().pending()[0]
        self.queue().decide(first_request.id, approved=True, by="Aditya (human)")

        second = self.make_agent()
        second.run_task(objective, acceptance=criteria, task_id=run.task_id, resume=True)
        self.assertEqual(self.queue().get(first_request.id).status, "consumed")

        # The model asks again, identically. One decision does not cover two executions.
        third = self.make_agent()
        again = third.run_task(objective, acceptance=criteria, task_id=run.task_id, resume=True)
        self.assertEqual(again.status, "awaiting_approval")
        waiting = [r for r in self.queue().recent(limit=10) if r.status == "pending"]
        self.assertEqual(len(waiting), 1, [r.render() for r in self.queue().recent(limit=10)])
        self.assertNotEqual(waiting[0].id, first_request.id)

    def test_a_rejected_operation_is_not_carried_out(self) -> None:
        target = self.restricted / "release.md"
        run = self.make_agent().run_task(
            "write the release file into the restricted directory",
            acceptance=[{"kind": "file_exists", "path": str(target)}])
        request = self.queue().pending()[0]
        self.queue().decide(request.id, approved=False, by="Aditya (human)",
                            note="hold this release")

        second = self.make_agent()
        resumed = second.run_task("write the release file into the restricted directory",
                                  acceptance=[{"kind": "file_exists", "path": str(target)}],
                                  task_id=run.task_id, resume=True)
        self.assertFalse(target.exists(), "a rejection must not be turned into a write")
        self.assertIn(resumed.status, ("awaiting_approval", "blocked", "partial"))
        self.assertEqual(second.tools.approvals.get(request.id).status, "rejected")


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
