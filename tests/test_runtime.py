import tempfile
import unittest
import json
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from runtime import ManagedRuntime, RuntimeError, RuntimeRejected
from service import Store


class FakeConnection:
    def __init__(self):
        self.loaded = set()
        self.calls = []
        self.replies = []

    def request(self, method, params, timeout=15):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "managed-thread", "name": "test managed"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        if method == "thread/read":
            return {"thread": {"turns": self.replies}}
        return {}

    def send(self, message):
        self.calls.append(("response", message))

    def close(self):
        pass


class ManagedRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "managed"
        self.root.mkdir()
        self.store = Store(self.root)
        self.store.init("managed", "test")
        self.task = self.store.call("task_create", {
            "id": "TASK-1", "title": "isolated test", "body": "test only",
            "request_id": "task-1"})
        self.runtime = ManagedRuntime()
        self.fake = FakeConnection()
        self.runtime.connection = self.fake

    def action(self, action, rid, **extra):
        return self.runtime.action(self.store, {
            "action": action, "request_id": rid, "task_id": "TASK-1", **extra})

    def test_task_context_is_read_only_bounded_and_fingerprint_checks_current_task(self):
        original = self.store.call("project_status")["sequence"]
        first = self.runtime.context(self.store, "TASK-1")
        self.assertTrue(first["eligible"])
        self.assertEqual(first["task"]["spec"], "test only")
        self.assertEqual(first["task"]["checks"], ["technical"])
        self.assertEqual(self.store.call("project_status")["sequence"], original)
        self.assertEqual(self.fake.calls, [])
        self.action("create", "context-create", context_fingerprint=first["fingerprint"])
        self.assertEqual(next(c[1] for c in self.fake.calls if c[0] == "thread/start")["model"], "gpt-6-luna")
        self.store.call("task_progress", {"id": "TASK-1", "expected_version": self.task["version"],
                        "body": "changed scope; stop before writes", "request_id": "new-scope"})
        old_calls = len(self.fake.calls)
        with self.assertRaisesRegex(RuntimeError, "上下文已变化"):
            self.action("continue", "stale-context", thread_id="managed-thread", text="hello",
                        context_fingerprint=first["fingerprint"])
        self.assertEqual(len(self.fake.calls), old_calls)
        fresh = self.runtime.context(self.store, "TASK-1")
        result = self.action("continue", "fresh-context", thread_id="managed-thread", text="hello",
                             context_fingerprint=fresh["fingerprint"])
        request = next(c[1] for c in self.fake.calls if c[0] == "turn/start")
        submitted = request["input"][0]["text"]
        self.assertIn("changed scope; stop before writes", submitted)
        self.assertTrue(submitted.endswith("[本次明确指令]\nhello"))
        self.assertEqual(result["context_fingerprint"], fresh["fingerprint"])
        self.assertEqual(result["prompt_bytes"], len(submitted.encode()))
        operation = next(e for e in self.runtime._project(self.store)[1]["operations"].values()
                         if e["request_id"] == "fresh-context")
        # Dispatch snapshot is in the original journal even when operation projection stores final response.
        journal = (self.store.folder / "runtime-events.jsonl").read_text()
        self.assertIn(fresh["fingerprint"], journal)

    def test_pending_human_gate_and_large_scope_cannot_dispatch_or_be_silently_cut(self):
        self.store.call("human_gate_request", {"id": "STOP", "title": "stop here", "task_id": "TASK-1",
                                              "body": "explicit approval required", "request_id": "stop"})
        c = self.runtime.context(self.store, "TASK-1")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["human_gates"][0]["status"], "pending")
        self.assertIn("explicit approval required", c["human_gates"][0]["body"])
        with self.assertRaises(RuntimeError):
            self.action("create", "blocked-create")
        self.assertEqual(self.fake.calls, [])
        self.store.call("task_create", {"id": "BIG", "title": "large", "body": "范围" * 4000,
                                       "request_id": "big"})
        with self.assertRaisesRegex(RuntimeError, "必要任务上下文超过"):
            self.runtime.context(self.store, "BIG")

    def test_context_preserves_hard_dependencies_and_optional_omission(self):
        dep = self.store.call("task_create", {"id": "DEP", "title": "prior", "body": "prior body", "request_id": "dep"})
        self.store.call("relation_add", {"source_id": "TASK-1", "expected_version": 1, "relation": "depends_on", "target_id": "DEP",
                                   "basis": "real prerequisite", "request_id": "dep-edge"})
        c = self.runtime.context(self.store, "TASK-1")
        self.assertFalse(c["eligible"])
        self.assertEqual(c["dependencies"][0]["id"], "DEP")
        self.assertEqual(c["dependencies"][0]["status"], "todo")
        self.assertIn("等待 DEP", c["reasons"])
        self.assertEqual(c["omitted_optional_refs"], 0)

    def test_context_change_between_preflight_and_rpc_is_definitely_rejected(self):
        self.action("create", "race-create")
        real = self.runtime.context
        count = 0
        def race(store, task_id):
            nonlocal count
            result = real(store, task_id)
            count += 1
            if count == 1:
                self.store.call("task_progress", {"id": "TASK-1", "expected_version": self.task["version"],
                                "body": "updated before RPC", "request_id": "rpc-race"})
            return result
        self.runtime.context = race
        result = self.action("continue", "race-turn", thread_id="managed-thread", text="hello")
        self.assertTrue(result.get("rejected"))
        self.assertFalse(any(c[0] == "turn/start" for c in self.fake.calls))

    def test_changed_prerequisite_file_blocks_rpc_without_node_version_change(self):
        source = self.root / "prior-proof.md"
        source.write_text("verified prior")
        dep = self.store.call("task_create", {"id": "DEP-FILE", "title": "verified prior",
                              "body": "prior", "checks": ["file"], "request_id": "dep-file"})
        dep = self.store.call("task_claim", {"id": dep["id"], "expected_version": dep["version"],
                                             "request_id": "dep-file-claim"})
        self.store.call("validation_record", {"id": dep["id"], "expected_version": dep["version"],
                        "check": "file", "result": "PASS", "body": "actual fixture proof",
                        "source": {"uri": str(source), "locator": "fixture"}, "request_id": "dep-proof"})
        self.store.call("task_complete", {"id": dep["id"], "expected_version": dep["version"],
                        "request_id": "dep-complete"})
        self.store.call("relation_add", {"source_id": "TASK-1", "expected_version": 1, "relation": "depends_on", "target_id": dep["id"],
                                   "basis": "validated prior", "request_id": "dep-file-edge"})
        self.action("create", "file-race-create")
        before = self.runtime.context(self.store, "TASK-1")
        real = self.runtime.context
        count = 0
        def race(store, task_id):
            nonlocal count
            result = real(store, task_id)
            count += 1
            if count == 1:
                source.write_text("changed after preflight")
            return result
        self.runtime.context = race
        result = self.action("continue", "file-race-turn", thread_id="managed-thread", text="hello")
        self.assertTrue(result.get("rejected"))
        self.assertFalse(any(c[0] == "turn/start" for c in self.fake.calls))
        after = real(self.store, "TASK-1")
        self.assertEqual(before["fingerprint"], after["fingerprint"])
        self.assertFalse(after["eligible"])

    def test_interrupt_after_new_stop_gate_does_not_allow_new_execution(self):
        self.action("create", "stop-create")
        self.action("continue", "stop-start", thread_id="managed-thread", text="pure test")
        current = self.store.call("graph_get", {"id": "TASK-1"})["node"]
        self.store.call("task_block", {"id": "TASK-1", "expected_version": current["version"],
                                     "body": "new human stop; no further execution", "request_id": "new-stop"})
        result = self.action("interrupt", "stop-interrupt", thread_id="managed-thread", turn_id="turn-1")
        self.assertEqual(result["status"], "interruptRequested")
        with self.assertRaises(RuntimeError):
            self.action("continue", "blocked-new-start", thread_id="managed-thread", text="do not start")
        self.assertEqual(sum(c[0] == "turn/start" for c in self.fake.calls), 1)
        self.assertEqual(self.store.call("graph_get", {"id": "TASK-1"})["node"]["status"], "blocked")

    def test_create_continue_idempotency_and_task_link(self):
        created = self.action("create", "create-1", model="gpt-6-luna")
        self.assertEqual(created["thread_id"], "managed-thread")
        self.assertTrue(created["linked"])
        self.assertEqual(self.action("create", "create-1", model="gpt-6-luna"), created)
        self.assertEqual([c[0] for c in self.fake.calls].count("thread/start"), 1)
        result = self.action("continue", "turn-1", thread_id="managed-thread", text="say hello")
        self.assertEqual(result["turn_id"], "turn-1")
        self.assertEqual(self.action("continue", "turn-1", thread_id="managed-thread", text="say hello"), result)
        self.assertEqual([c[0] for c in self.fake.calls].count("turn/start"), 1)
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "inProgress")
        run = self.store.call("graph_get", {"id": "run-turn-1"})["node"]
        self.assertEqual(run["data"]["runtime_status"], "inProgress")
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "completed"}}})
        run = self.store.call("graph_get", {"id": "run-turn-1"})["node"]
        self.assertEqual(run["data"]["runtime_status"], "completed")
        self.assertNotEqual(self.store.call("graph_get", {"id": "TASK-1"})["node"]["status"], "completed")

    def test_reject_foreign_thread_and_stale_request_without_dispatch(self):
        with self.assertRaises(RuntimeError):
            self.action("continue", "foreign", thread_id="desktop-thread", text="unsafe")
        self.action("create", "create-1")
        with self.assertRaises(RuntimeError):
            self.action("create", "create-1", model="gpt-6-luna")
        with self.assertRaises(RuntimeError):
            self.action("continue", "empty", thread_id="managed-thread", text="  ")
        self.assertEqual([c[0] for c in self.fake.calls].count("turn/start"), 0)

    def test_disconnect_unknown_reconcile_and_explicit_approval(self):
        self.action("create", "create-1")
        self.action("continue", "turn-1", thread_id="managed-thread", text="say hello")
        self.runtime._on_message({"method": "item/commandExecution/requestApproval",
                                  "id": 17, "params": {"threadId": "managed-thread",
                                                       "turnId": "turn-1", "command": "echo test"}})
        with self.assertRaises(RuntimeError):
            self.action("approval", "wrong", thread_id="managed-thread",
                        turn_id="other", approval_request_id="17", decision="accept")
        approved = self.action("approval", "approve-1", thread_id="managed-thread",
                               turn_id="turn-1", approval_request_id="17", decision="decline")
        self.assertEqual(approved["decision"], "decline")
        self.assertEqual(self.fake.calls[-1], ("response", {"id": 17,
                                                              "result": {"decision": "decline"}}))
        self.runtime._on_message({"method": "connection/closed", "params": {}})
        with self.assertRaises(RuntimeError):
            self.action("continue", "blind", thread_id="managed-thread", text="again")
        self.fake.replies = [{"id": "turn-1", "status": "completed"}]
        result = self.action("reconcile", "reconcile-1", thread_id="managed-thread")
        self.assertEqual(result["status"], "completed")
        restarted = ManagedRuntime()
        self.assertEqual(restarted.status(self.store)["threads"][0]["status"], "completed")

    def test_explicit_app_server_rejection_is_distinct_from_uncertain_dispatch(self):
        self.action("create", "create-1")
        self.action("continue", "turn-1", thread_id="managed-thread", text="say hello")
        original_request = self.fake.request

        def reject_interrupt(method, params, timeout=15):
            if method == "turn/interrupt":
                raise RuntimeRejected("no active turn to interrupt")
            return original_request(method, params, timeout)

        self.fake.request = reject_interrupt
        rejected = self.action("interrupt", "interrupt-rejected", thread_id="managed-thread",
                               turn_id="turn-1")
        self.assertEqual(rejected, {"rejected": True, "error": "no active turn to interrupt"})
        self.assertEqual(self.action("interrupt", "interrupt-rejected", thread_id="managed-thread",
                                     turn_id="turn-1"), rejected)
        events = [json.loads(line) for line in
                  (self.store.folder / "runtime-events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "rejected")

        def timeout_interrupt(method, params, timeout=15):
            if method == "turn/interrupt":
                raise RuntimeError("App Server 响应超时；不能盲目重发")
            return original_request(method, params, timeout)

        self.fake.request = timeout_interrupt
        uncertain = self.action("interrupt", "interrupt-timeout", thread_id="managed-thread",
                                turn_id="turn-1")
        self.assertTrue(uncertain["uncertain"])
        self.assertNotIn("rejected", uncertain)
        events = [json.loads(line) for line in
                  (self.store.folder / "runtime-events.jsonl").read_text().splitlines()]
        self.assertEqual(events[-1]["status"], "unknown")

    def test_failure_reason_survives_replay_and_read_reconciliation(self):
        self.action("create", "create-failure")
        self.action("continue", "start-failure", thread_id="managed-thread", text="test")
        turn = {"id": "turn-1", "status": "failed",
                "error": {"message": "model not supported"}}
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": turn}})
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["reason"],
                         "model not supported")
        self.assertEqual(ManagedRuntime().status(self.store)["threads"][0]["reason"],
                         "model not supported")
        self.fake.replies = [turn]
        self.action("reconcile", "read-failure", thread_id="managed-thread")
        run = self.store.call("graph_get", {"id": "run-turn-1"})["node"]
        self.assertEqual(run["data"]["runtime_reason"], "model not supported")
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "completed"}}})
        self.assertIsNone(self.runtime.status(self.store)["threads"][0]["reason"])

    def test_unoffered_approval_decision_never_dispatches(self):
        self.action("create", "create-approval")
        self.action("continue", "start-approval", thread_id="managed-thread", text="test")
        self.runtime._on_message({"method": "item/commandExecution/requestApproval",
            "id": 19, "params": {"threadId": "managed-thread", "turnId": "turn-1",
                "availableDecisions": ["accept", "cancel"]}})
        before = len(self.fake.calls)
        with self.assertRaises(RuntimeError):
            self.action("approval", "unsupported", thread_id="managed-thread",
                        turn_id="turn-1", approval_request_id="19", decision="decline")
        self.assertEqual(len(self.fake.calls), before)
        result = self.action("approval", "cancel-offered", thread_id="managed-thread",
            turn_id="turn-1", approval_request_id="19", decision="cancel")
        self.assertEqual(result["decision"], "cancel")


    def test_stale_approval_notification_never_reopens_terminal_or_unknown_turn(self):
        self.action("create", "capture-create")
        self.action("continue", "capture-turn", thread_id="managed-thread", text="test")
        request = {"id": "approve-string", "method": "item/commandExecution/requestApproval",
                   "params": {"threadId": "managed-thread", "turnId": "old-turn", "command": "true"}}
        self.runtime._on_message(request)
        self.assertEqual(self.runtime.status(self.store)["pending_approvals"], [])
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "completed"}}})
        request["params"]["turnId"] = "turn-1"
        self.runtime._on_message(request)
        self.assertEqual(self.runtime.status(self.store)["pending_approvals"], [])
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "completed")

    def test_approval_string_rpc_and_unknown_capacity_cannot_be_answered(self):
        self.action("create", "capture-string-create")
        self.action("continue", "capture-string-turn", thread_id="managed-thread", text="test")
        def request(key, command):
            self.runtime._on_message({"id": key, "method": "item/commandExecution/requestApproval",
                "params": {"threadId": "managed-thread", "turnId": "turn-1", "command": command,
                           "availableDecisions": ["accept", "cancel"]}})
        request("approve-string", "true")
        self.action("approval", "string-decision", thread_id="managed-thread", turn_id="turn-1",
                    approval_request_id="approve-string", decision="cancel")
        self.assertEqual(self.fake.calls[-1], ("response", {"id": "approve-string", "result": {"decision": "cancel"}}))
        request("pending-old", "true")
        request("too-large", "x" * 32769)
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "unknown")
        before = len(self.fake.calls)
        with self.assertRaises(RuntimeError):
            self.action("approval", "unknown-decision", thread_id="managed-thread", turn_id="turn-1",
                        approval_request_id="pending-old", decision="accept")
        self.assertEqual(len(self.fake.calls), before)

    def test_live_output_is_bounded_scoped_and_does_not_write_per_delta(self):
        self.action("create", "output-create")
        self.action("continue", "output-turn", thread_id="managed-thread", text="test")
        journal = self.store.folder / "runtime-events.jsonl"
        before = journal.stat().st_size
        def delta(turn, item, text):
            self.runtime._on_message({"method": "item/agentMessage/delta", "params": {
                "threadId": "managed-thread", "turnId": turn, "itemId": item, "delta": text}})
        delta("old-turn", "output-old", "wrong")
        self.assertFalse(self.runtime.status(self.store)["threads"][0].get("output"))
        delta("turn-1", "output-1", "a" * 5000)
        delta("turn-1", "output-1", "<visible>")
        first = self.runtime.status(self.store)["threads"][0]["output"]
        self.assertEqual(len(first["text"]), 1024)
        self.assertTrue(first["truncated"])
        self.assertTrue(first["text"].endswith("<visible>"))
        first["text"] = "external mutation"
        self.assertNotEqual(self.runtime.status(self.store)["threads"][0]["output"]["text"], first["text"])
        self.assertEqual(journal.stat().st_size, before)
        delta("turn-1", "output-2", "new output")
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["output"]["text"], "new output")
        self.runtime._on_message({"method": "item/completed", "params": {"threadId": "managed-thread",
            "turnId": "turn-1", "item": {"type": "agentMessage", "id": "output-2", "text": "final output"}}})
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "completed"}}})
        restored = ManagedRuntime().status(self.store)["threads"][0]["output"]
        self.assertEqual(restored["text"], "final output")
        self.assertTrue(restored["complete"])
        delta("turn-1", "output-2", "late")
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["output"]["text"], "final output")
        self.assertNotEqual(self.store.call("graph_get", {"id": "TASK-1"})["node"]["status"], "completed")

    def test_close_saves_bounded_live_tail_as_unknown_without_resuming(self):
        self.action("create", "close-create")
        self.action("continue", "close-turn", thread_id="managed-thread", text="test")
        self.runtime._on_message({"method": "item/agentMessage/delta", "params": {
            "threadId": "managed-thread", "turnId": "turn-1", "itemId": "output-1", "delta": "partial tail"}})
        before = len(self.fake.calls)
        self.runtime.close()
        restored = ManagedRuntime().status(self.store)["threads"][0]
        self.assertEqual(restored["status"], "unknown")
        self.assertEqual(restored["output"]["text"], "partial tail")
        self.assertFalse(restored["output"]["complete"])
        self.assertEqual(len(self.fake.calls), before)


    def test_late_prior_turn_completion_cannot_replace_current_output_or_status(self):
        self.action("create", "late-create")
        self.action("continue", "late-start", thread_id="managed-thread", text="test")
        self.runtime._on_message({"method":"turn/started","params":{"threadId":"managed-thread",
            "turn":{"id":"turn-2","status":"inProgress"}}})
        self.runtime._on_message({"method":"item/agentMessage/delta","params":{"threadId":"managed-thread",
            "turnId":"turn-2","itemId":"item-2","delta":"current"}})
        self.runtime._on_message({"method":"turn/completed","params":{"threadId":"managed-thread",
            "turn":{"id":"turn-1","status":"completed"}}})
        current=self.runtime.status(self.store)["threads"][0]
        self.assertEqual(current["turn_id"],"turn-2")
        self.assertEqual(current["status"],"inProgress")
        self.assertEqual(current["output"]["text"],"current")


    def test_new_turn_reply_clears_old_tail_before_any_notification(self):
        self.action("create","reply-tail-create")
        self.action("continue","reply-tail-first",thread_id="managed-thread",text="test")
        self.runtime._on_message({"method":"item/completed","params":{"threadId":"managed-thread",
            "turnId":"turn-1","item":{"id":"item-1","type":"agentMessage","text":"old completed output"}}})
        self.runtime._on_message({"method":"turn/completed","params":{"threadId":"managed-thread",
            "turn":{"id":"turn-1","status":"completed"}}})
        original=self.fake.request
        def next_turn(method,params,timeout=15):
            if method=="turn/start":
                return {"turn":{"id":"turn-2","status":"inProgress"}}
            return original(method,params,timeout)
        self.fake.request=next_turn
        self.action("continue","reply-tail-second",thread_id="managed-thread",text="test new round")
        state=self.runtime.status(self.store)["threads"][0]
        self.assertEqual(state["turn_id"],"turn-2")
        self.assertEqual(state["status"],"inProgress")
        self.assertIsNone(state.get("output"))


    def user_input(self, rpc_id="question-seven", blocking=True, **question_updates):
        self.runtime._on_message({"id": rpc_id, "method": "item/tool/requestUserInput", "params": {
            "threadId": "managed-thread", "turnId": "turn-1", "itemId": "question-item",
            "isBlocking": blocking, "autoResolutionMs": 1,
            "questions": [{"id": "choice", "header": "展示", "question": "Choose <A> or B?",
                           "options": [{"label": "A", "description": "A display"},
                                       {"label": "B", "description": "B display"}], **question_updates}]}})

    def test_input_exact_identity_explicit_answers_and_no_secret_log(self):
        self.action("create", "create-input")
        self.action("continue", "start-input", thread_id="managed-thread", text="question")
        self.user_input()
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "waitingInput")
        before = len(self.fake.calls)
        for fields in [{"turn_id": "other", "answers": {"choice": {"answers": ["A"]}}},
                       {"turn_id": "turn-1", "answers": {}},
                       {"turn_id": "turn-1", "answers": {"choice": {"answers": [""]}}},
                       {"turn_id": "turn-1", "answers": {"choice": {"answers": ["C"]}}}]:
            with self.assertRaises(RuntimeError):
                self.action("input", "invalid-"+str(fields), thread_id="managed-thread",
                            input_request_id="question-seven", **fields)
        self.assertEqual(len(self.fake.calls), before)
        answer = {"choice": {"answers": ["B"]}}
        result = self.action("input", "answer-once", thread_id="managed-thread", turn_id="turn-1",
                             input_request_id="question-seven", answers=answer)
        self.assertTrue(result["answered"])
        self.assertEqual(self.fake.calls[-1], ("response", {"id": "question-seven", "result": {"answers": answer}}))
        self.assertEqual(self.action("input", "answer-once", thread_id="managed-thread", turn_id="turn-1",
                         input_request_id="question-seven", answers=answer), result)
        self.assertEqual(len(self.fake.calls), before+1)
        self.assertEqual(self.runtime.status(self.store)["pending_inputs"], [])
        self.assertNotIn('"answers"', (self.store.folder / "runtime-events.jsonl").read_text())

    def test_nonblocking_input_and_resolved_notification_do_not_cross_threads(self):
        self.action("create", "create-nonblocking")
        self.action("continue", "start-nonblocking", thread_id="managed-thread", text="question")
        self.user_input(blocking=False)
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "inProgress")
        self.runtime._on_message({"method": "serverRequest/resolved", "params": {
            "threadId": "foreign", "requestId": "question-seven"}})
        self.assertEqual(len(self.runtime.status(self.store)["pending_inputs"]), 1)
        self.runtime._on_message({"method": "serverRequest/resolved", "params": {
            "threadId": "managed-thread", "requestId": "question-seven"}})
        self.assertEqual(self.runtime.status(self.store)["pending_inputs"], [])

    def test_input_disconnect_and_restart_require_reconcile_without_reanswer(self):
        self.action("create", "create-disconnected-input")
        self.action("continue", "start-disconnected-input", thread_id="managed-thread", text="question")
        self.user_input()
        restarted = ManagedRuntime()
        self.assertEqual(restarted.status(self.store)["threads"][0]["status"], "unknown")
        self.assertEqual(restarted.status(self.store)["pending_inputs"], [])
        self.runtime._on_message({"method": "connection/closed", "params": {}})
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "unknown")
        self.assertEqual(self.runtime.status(self.store)["pending_inputs"], [])
        before = len(self.fake.calls)
        with self.assertRaises(RuntimeError):
            self.action("input", "stale-input", thread_id="managed-thread", turn_id="turn-1",
                        input_request_id="question-seven", answers={"choice": {"answers": ["A"]}})
        self.assertEqual(len(self.fake.calls), before)
        self.fake.replies = [{"id": "turn-1", "status": "interrupted"}]
        self.assertEqual(self.action("reconcile", "recover-input", thread_id="managed-thread")["status"], "interrupted")

    def test_terminal_and_sensitive_input_never_get_default_answer(self):
        self.action("create", "create-secret-input")
        self.action("continue", "start-secret-input", thread_id="managed-thread", text="question")
        self.user_input(isSecret=True)
        with self.assertRaises(RuntimeError):
            self.action("input", "secret-input", thread_id="managed-thread", turn_id="turn-1",
                        input_request_id="question-seven", answers={"choice": {"answers": ["A"]}})
        self.runtime._on_message({"method": "turn/completed", "params": {
            "threadId": "managed-thread", "turn": {"id": "turn-1", "status": "interrupted"}}})
        self.assertEqual(self.runtime.status(self.store)["pending_inputs"], [])
        self.user_input()
        self.assertEqual(self.runtime.status(self.store)["pending_inputs"], [])
        self.assertEqual(self.runtime.status(self.store)["threads"][0]["status"], "interrupted")

    def test_only_explicit_plan_mode_changes_turn_settings(self):
        self.action("create", "create-plan", model="gpt-5.6-terra")
        before = len(self.fake.calls)
        with self.assertRaises(RuntimeError):
            self.action("continue", "bad-mode", thread_id="managed-thread", text="question", interaction_mode="auto")
        self.assertEqual(len(self.fake.calls), before)
        self.action("continue", "start-plan", thread_id="managed-thread", text="question", interaction_mode="plan")
        request = self.fake.calls[-1]
        self.assertEqual(request[1]["collaborationMode"], {"mode": "plan", "settings": {"model": "gpt-5.6-terra"}})
        self.assertFalse(request[1]["sandboxPolicy"]["networkAccess"])

    def test_http_requires_project_identity_and_rejects_foreign_thread(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        app = Path(__file__).resolve().parents[1] / "pgw.py"
        process = subprocess.Popen([sys.executable, str(app), "--root", str(self.root),
                                    "serve", "--port", str(port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertIn("http://", process.stdout.readline())
            base = f"http://127.0.0.1:{port}"
            session = json.load(urllib.request.urlopen(base + "/api/session"))
            identity = self.store.call("project_open")["project"]["data"]["project_id"]
            status = json.load(urllib.request.urlopen(base + "/api/runtime?project=" + identity))
            self.assertEqual(status["threads"], [])
            def post(data):
                request = urllib.request.Request(base + "/api/runtime", data=json.dumps(data).encode(),
                    headers={"Content-Type": "application/json", "X-PGW-CSRF": session["csrf"]})
                return urllib.request.urlopen(request)
            with self.assertRaises(urllib.error.HTTPError) as missing:
                post({"action": "continue", "request_id": "missing",
                      "task_id": "TASK-1", "thread_id": "desktop", "text": "x"})
            self.assertEqual(missing.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as foreign:
                post({"project_id": identity, "action": "continue", "request_id": "foreign",
                      "task_id": "TASK-1", "thread_id": "desktop", "text": "x"})
            self.assertEqual(foreign.exception.code, 409)
            self.assertFalse((self.store.folder / "runtime-events.jsonl").exists())
        finally:
            process.terminate()
            process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
