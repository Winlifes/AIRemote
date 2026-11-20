#!/usr/bin/env python3
"""Local JSON-RPC client for official Codex app-server metadata/new threads.

Existing desktop turns are NEVER resumed or executed here. They are controlled
by DesktopIPC through their existing owner. This process manages native queue
records and creates genuinely new threads only when explicitly requested.
"""

from collections import deque
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import threading
import time


class RPCError(RuntimeError):
    def __init__(self, method, error):
        self.method = method
        self.error = error
        super().__init__(method + ": " + str(error.get("message", error)))


def find_codex_installation():
    for bundle in map(Path, ('/Applications/ChatGPT.app', '/Applications/Codex.app')):
        try:
            info = plistlib.loads((bundle / 'Contents/Info.plist').read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            continue
        if isinstance(info, dict) and info.get('CFBundleIdentifier') == 'com.openai.codex':
            cli = bundle / 'Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
            return bundle, info, cli
    raise FileNotFoundError('请先安装 Codex 桌面客户端。')


def find_codex_executable(executable=None):
    """Use the bundled App Server when it is available on this Mac."""
    if executable:
        # Preserve explicit test/embedded transports; Popen validates on entry.
        return str(executable)
    try:
        _, _, candidate = find_codex_installation()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    except FileNotFoundError:
        pass
    candidate = shutil.which('codex')
    if candidate:
        return candidate
    raise FileNotFoundError('Codex CLI is not available; install/open the Codex desktop app first')


class AppServerRPC:
    def __init__(self, executable=None):
        self.executable = find_codex_executable(executable)
        self.process = None
        self.condition = threading.Condition()
        self.write_lock = threading.Lock()
        self.responses = {}
        self.events = deque(maxlen=2000)
        self.server_requests = deque(maxlen=50)
        self.stderr_tail = deque(maxlen=20)
        self.next_id = 1
        self.closed = False
        self.created_threads = set()

    def __enter__(self):
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.initialized = self.request("initialize", {
                "clientInfo": {"name": "airemote_local", "title": "AIRemote local connector", "version": "0.2.0"},
                "capabilities": {"experimentalApi": True},
            }, timeout=30)
            self._write({"method": "initialized"})
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def _write(self, value):
        with self.write_lock:
            if self.process.poll() is not None:
                raise ConnectionError("Codex app-server exited")
            self.process.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
            self.process.stdin.flush()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                with self.condition:
                    if "id" in value and "method" not in value:
                        self.responses[value["id"]] = value
                    elif "id" in value and "method" in value:
                        # Preserve requests for explicit handling; never silently
                        # approve a command or change the user's permission mode.
                        self.server_requests.append(value)
                    else:
                        self.events.append(value)
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr_tail.append(line.rstrip())

    def request(self, method, params=None, timeout=30):
        if method in {"thread/resume", "turn/start", "turn/steer", "thread/queue/start"}:
            target = (params or {}).get("threadId")
            if target not in self.created_threads:
                raise PermissionError("Existing desktop threads must use their original IPC owner")
        with self.condition:
            request_id = self.next_id
            self.next_id += 1
        self._write({"id": request_id, "method": method, "params": params or {}})
        deadline = time.monotonic() + timeout
        with self.condition:
            while request_id not in self.responses:
                if self.closed:
                    raise ConnectionError("Codex app-server closed during " + method)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Response timed out; do not blindly retry: " + method)
                self.condition.wait(remaining)
            response = self.responses.pop(request_id)
        if "error" in response:
            raise RPCError(method, response["error"])
        result = response.get("result", {})
        if method == "thread/start":
            self.created_threads.add(result["thread"]["id"])
        return result

    def wait_turn(self, thread_id, turn_id, timeout=90):
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                for event in self.events:
                    params = event.get("params", {})
                    turn = params.get("turn", {})
                    if (event.get("method") == "turn/completed"
                            and params.get("threadId") == thread_id
                            and turn.get("id") == turn_id):
                        return turn
                if self.closed:
                    raise ConnectionError("Codex exited before completing the new turn")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("New turn has not completed")
                self.condition.wait(remaining)

    def close(self):
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for stream in [self.process.stdout, self.process.stderr]:
            if stream:
                stream.close()


class NativeQueue:
    def __init__(self, rpc):
        self.rpc = rpc

    def list(self, thread_id):
        items = []
        cursor = None
        while True:
            result = self.rpc.request("thread/queue/list", {"threadId": thread_id, "cursor": cursor})
            items.extend(result["data"])
            cursor = result.get("nextCursor")
            if cursor is None:
                return items

    def add(self, thread_id, text, client_message_id):
        return self.rpc.request("thread/queue/add", {
            "threadId": thread_id, "clientUserMessageId": client_message_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        })["queuedSubmission"]

    def update(self, thread_id, item_id, text):
        return self.rpc.request("thread/queue/update", {
            "threadId": thread_id, "queuedSubmissionId": item_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        })["queuedSubmission"]

    def delete(self, thread_id, item_id):
        return self.rpc.request("thread/queue/delete", {
            "threadId": thread_id, "queuedSubmissionId": item_id,
        })["deleted"]

    def reorder(self, thread_id, ids):
        return self.rpc.request("thread/queue/reorder", {
            "threadId": thread_id, "queuedSubmissionIds": ids,
        })

    def clear(self, thread_id, ids=None):
        items = self.list(thread_id)
        selected = {i["id"] for i in items} if ids is None else set(ids)
        # Remove only this explicitly observed selection. Newly added entries
        # are not swept up by a second query during deletion.
        return [item["id"] for item in items
                if item["id"] in selected and self.delete(thread_id, item["id"])]
