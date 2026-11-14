import json
from pathlib import Path
import secrets
import socket
import struct
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app_server_rpc import AppServerRPC
from control_server import Conflict, Controller, ControlServer, Journal, desktop_model_settings
from desktop_ipc import DesktopError, DesktopIPC, MAX_FRAME_BYTES


THREAD = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class FakeRPC:
    def __init__(self):
        self.created_threads = set()
        self.calls = []

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/queue/list":
            return {"data": [], "nextCursor": None}
        raise AssertionError("Unexpected RPC call: " + method)


class FakeHistory:
    def __init__(self, thread_id):
        self.thread_id = thread_id

    def snapshot(self):
        return {"threadId": self.thread_id, "turns": []}

    def messages(self, *_):
        return []

    def delivery_receipt(self, _):
        return {'receivedInHistory': False}


class WriteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.calls = []
        self.failure = None
        parent = self

        class Desktop:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def send(self, thread_id, text, client_id):
                parent.calls.append((thread_id, text, client_id))
                if parent.failure:
                    raise parent.failure
                return {"result": {"result": {"turnId": "existing-turn"}}, "handledByClientId": "original-owner"}

        self.rpc = FakeRPC()
        self.controller = Controller([THREAD], [self.root], [self.root], self.root / "state", self.rpc, Desktop)
        self.controller.history = lambda tid: FakeHistory(self.controller.allowed(tid))
        self.token = secrets.token_urlsafe(32)
        self.server = ControlServer(0, self.controller, self.token)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()
        self.controller.close()
        self.temp.cleanup()

    def request(self, method, path, body=None, key="test-key", authenticate=True):
        headers = {"Content-Type": "application/json"}
        if authenticate:
            headers["Authorization"] = "Bearer " + self.token
        if key is not None:
            headers["Idempotency-Key"] = key
        data = json.dumps(body or {}).encode() if method != "GET" else None
        with urlopen(Request(self.base + path, data=data, headers=headers, method=method), timeout=3) as response:
            return response.status, json.load(response)

    def test_queue_storage_is_not_execution_acceptance(self):
        self.controller.queue.add = lambda tid, text, client: {"id": OTHER, "clientUserMessageId": client,
            "input": [{"type": "text", "text": text}]}
        _, result = self.request("POST", "/api/threads/" + THREAD + "/queue", {"text": "queued"})
        self.assertEqual(result["result"]["deliveryState"], "queued")
        self.assertFalse(result["delivery"]["accepted"])
        self.assertFalse(result["delivery"]["receivedInHistory"])
        self.assertTrue(result["result"]["clientUserMessageId"])

    def test_capabilities_only_reports_authorized_workspaces(self):
        _, result = self.request("GET", "/api/capabilities")
        self.assertEqual(result["workspaces"], [str(self.root.resolve())])
        self.assertIn("existing-thread-write", result["features"])

    def test_agent_heartbeat_does_not_read_projects_or_history(self):
        def forbidden():
            raise AssertionError('Health must not load the project directory')
        self.controller.catalog.projects = forbidden
        _, result = self.request('GET', '/api/health')
        self.assertTrue(result['ok'])

    def test_session_includes_live_model_and_mode_without_private_context(self):
        class Reader:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def state(self, _):
                return {'ownerClientId': 'original-owner', 'conversationState': {
                    'threadRuntimeStatus': {'type': 'active'}, 'latestModel': 'old-model', 'latestReasoningEffort': None,
                    'latestCollaborationMode': {'mode': 'plan', 'settings': {'model': 'current-model', 'reasoning_effort': 'max', 'developer_instructions': 'PRIVATE INSTRUCTIONS'}},
                    'currentPermissions': {'sandboxPolicy': {'type': 'workspaceWrite'}, 'runtimeWorkspaceRoots': ['/private/root'], 'approvalPolicy': 'on-request'}}}
        self.controller.desktop_reader = Reader
        _, result = self.request('GET', '/api/threads/' + THREAD)
        self.assertEqual(result['modelSettings']['model'], 'current-model')
        self.assertEqual(result['modelSettings']['reasoningEffort'], 'max')
        self.assertEqual(result['modelSettings']['collaborationMode'], 'plan')
        self.assertEqual(result['modelSettings']['permissionMode'], 'workspaceWrite')
        self.assertNotIn('PRIVATE INSTRUCTIONS', json.dumps(result))
        self.assertNotIn('/private/root', json.dumps(result))

    def test_missing_live_settings_never_invents_model_or_access(self):
        result = desktop_model_settings({})
        self.assertIsNone(result['model'])
        self.assertIsNone(result['reasoningEffort'])
        self.assertIsNone(result['permissionMode'])

    def test_all_mutations_require_authentication(self):
        for method in ["POST", "PATCH", "DELETE", "PUT"]:
            with self.assertRaises(HTTPError) as error:
                self.request(method, "/api/threads/%s/messages" % THREAD, {"text": "do work"}, authenticate=False)
            self.assertEqual(error.exception.code, 401)
        self.assertEqual(self.calls, [])

    def test_duplicate_http_request_does_not_execute_twice(self):
        path = "/api/threads/%s/messages" % THREAD
        _, first = self.request("POST", path, {"text": "one command"})
        _, second = self.request("POST", path, {"text": "one command"})
        self.assertEqual(first["operationId"], second["operationId"])
        self.assertTrue(second["replayed"])
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(first["delivery"]["receivedInHistory"])

    def test_reused_key_with_different_instruction_is_rejected(self):
        path = "/api/threads/%s/messages" % THREAD
        self.request("POST", path, {"text": "first"})
        with self.assertRaises(HTTPError) as error:
            self.request("POST", path, {"text": "different"})
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(len(self.calls), 1)

    def test_mutation_without_idempotency_key_is_rejected(self):
        with self.assertRaises(HTTPError) as error:
            self.request("POST", "/api/threads/%s/messages" % THREAD, {"text": "first"}, key=None)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(self.calls, [])

    def test_unknown_delivery_is_not_retried(self):
        self.failure = TimeoutError("response lost after submission")
        path = "/api/threads/%s/messages" % THREAD
        with self.assertRaises(HTTPError) as error:
            self.request("POST", path, {"text": "one command"})
        self.assertEqual(error.exception.code, 409)
        status, replay = self.request("POST", path, {"text": "one command"})
        self.assertEqual(status, 202)
        self.assertEqual(replay["requestStatus"], "outcome_unknown")
        self.assertEqual(len(self.calls), 1)

    def test_other_threads_cannot_be_controlled(self):
        with self.assertRaises(HTTPError) as error:
            self.request("POST", "/api/threads/%s/messages" % OTHER, {"text": "do work"})
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.calls, [])

    def test_existing_thread_without_owner_is_never_resumed_in_sidecar(self):
        self.failure = DesktopError({"error": "no-client-found"})
        with self.assertRaises(Conflict):
            self.controller.perform("no-owner", "send", {"threadId": THREAD, "text": "test"})
        self.assertEqual(self.rpc.calls, [])

    def test_new_thread_cannot_escape_authorized_workspace(self):
        with self.assertRaises(PermissionError):
            self.controller.perform("escape", "create", {"cwd": "/", "text": "do work"})
        self.assertEqual(self.rpc.calls, [])

    def test_second_server_does_not_change_inflight_operation(self):
        operation, _ = self.controller.journal.begin("in-progress", "send", {"threadId": THREAD})
        with self.assertRaises(Conflict):
            Controller([THREAD], [self.root], [self.root], self.root / "state", self.rpc)
        self.assertEqual(self.controller.journal.get(operation["id"])["status"], "in_flight")

    def test_failed_live_status_keeps_history_readable_and_reports_unknown(self):
        class Unavailable:
            def __enter__(self): raise RuntimeError('new protocol')
            def __exit__(self, *_): pass
        self.controller.desktop_reader = Unavailable
        _, result = self.request('GET', '/api/threads/' + THREAD)
        self.assertEqual(result['threadId'], THREAD)
        self.assertIn('unavailable', result['runtimeStatusNote'])


class TransportSafetyTests(unittest.TestCase):
    def test_sidecar_refuses_to_execute_original_desktop_thread(self):
        rpc = AppServerRPC(executable="unused-in-this-test")
        for method in ["thread/resume", "turn/start", "turn/steer", "thread/queue/start"]:
            with self.assertRaises(PermissionError):
                rpc.request(method, {"threadId": THREAD})

    def test_stale_inflight_request_is_retained_as_unknown_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Journal(directory)
            operation, _ = journal.begin("restart-key", "send", {"threadId": THREAD})
            recovered = Journal(directory)
            existing, fresh = recovered.begin("restart-key", "send", {"threadId": THREAD})
            self.assertFalse(fresh)
            self.assertEqual(existing["id"], operation["id"])
            self.assertEqual(existing["status"], "outcome_unknown")

    def test_oversized_native_frame_is_rejected_before_reading_payload(self):
        left, right = socket.socketpair()
        client = DesktopIPC(socket_path="/unused/test-socket")
        client.socket = left
        try:
            right.sendall(struct.pack("<I", MAX_FRAME_BYTES + 1))
            with self.assertRaises(ValueError):
                client._read()
        finally:
            left.close()
            right.close()

    def test_unknown_desktop_version_keeps_reads_but_blocks_writes(self):
        directory = tempfile.TemporaryDirectory(prefix='arm-', dir='/tmp')
        socket_path = Path(directory.name) / 'ipc.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(socket_path)); socket_path.chmod(0o600); listener.listen(2)
        try:
            with patch('desktop_ipc.Path.read_bytes', return_value=b'ignored'), patch('desktop_ipc.plistlib.loads', return_value={'CFBundleShortVersionString': 'future.version'}):
                read = DesktopIPC(read_only=True)
                read.socket_path = socket_path
                with patch.object(read, 'request', return_value={'result': {'clientId': 'readonly-probe'}}):
                    with read:
                        self.assertEqual(read.client_id, 'readonly-probe')
                write = DesktopIPC()
                write.socket_path = socket_path
                with self.assertRaisesRegex(RuntimeError, 'needs validation'):
                    write.__enter__()
        finally:
            listener.close()
            directory.cleanup()

    def test_readonly_connection_cannot_dispatch_instructions(self):
        reader = DesktopIPC(socket_path='/unused', read_only=True)
        for method in ['thread-follower-start-turn', 'thread-follower-steer-turn', 'thread-follower-clear-queued-messages']:
            with self.assertRaises(PermissionError): reader.request(method, {})
            with self.assertRaises(PermissionError): reader.broadcast(method, {})

    def test_idle_existing_thread_starts_on_original_owner(self):
        class Desktop(DesktopIPC):
            def owner(self, _):
                return "original-owner"

            def state(self, *_):
                return {"conversationState": {"cwd": "/authorized/project", "threadRuntimeStatus": {"type": "idle"}}}

            def request(self, method, params, target=None, version=None):
                return {"method": method, "params": params, "target": target}

        result = Desktop(socket_path="/unused").send(THREAD, "continue", "client-id")
        self.assertEqual(result["method"], "thread-follower-start-turn")
        self.assertEqual(result["target"], "original-owner")
        self.assertEqual(result["params"]["turnStart"]["request"]["threadId"], THREAD)


if __name__ == "__main__":
    unittest.main()
