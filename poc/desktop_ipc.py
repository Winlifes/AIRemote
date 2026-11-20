#!/usr/bin/env python3
"""Client of the desktop's local conversation coordination bus.

Uses a same-user private socket and registers transparently as `airemote`.
It never impersonates an existing client, changes peer validation, opens the
restricted app-tools pipe, starts another app-server, or resumes a second agent.
The coordination protocol is an internal implementation detail, not a stable API.
"""

import argparse
from collections import deque
import json
import os
from pathlib import Path
import socket
import stat
import struct
import time
import uuid


MAX_FRAME_BYTES = 64 * 1024 * 1024
READ_METHODS = {'initialize', 'thread-owner-discovery', 'thread-follower-read-model-settings'}
VERSIONS = {
    "initialize": 0,
    "thread-owner-discovery": 1,
    "thread-follower-steer-turn": 1,
    "thread-follower-start-turn": 2,
    "thread-follower-load-complete-history": 1,
    "thread-follower-read-model-settings": 1,
    "thread-follower-set-queued-follow-ups-state": 1,
    "thread-follower-remove-queued-message": 1,
    "thread-follower-clear-queued-messages": 1,
    "thread-stream-following-changed": 1,
}


class DesktopError(RuntimeError):
    def __init__(self, response):
        self.response = response
        super().__init__(str(response.get("error", "Desktop request failed")))


class DesktopIPC:
    def __init__(self, socket_path=None, timeout=15, read_only=False):
        self.socket_path = Path(socket_path or Path.home() / ".codex/ipc/ipc.sock")
        self.timeout = timeout
        self.read_only = read_only
        self.client_id = "initializing-client"
        self.events = deque(maxlen=100)
        self.subscriptions = []
        self.socket = None

    def __enter__(self):
        node = self.socket_path.lstat()
        parent = self.socket_path.parent.stat()
        if not stat.S_ISSOCK(node.st_mode) or node.st_uid != os.getuid() or node.st_mode & 0o077:
            raise PermissionError("Desktop socket must belong to this user")
        if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
            raise PermissionError("Desktop socket directory must be private")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(self.timeout)
        try:
            self.socket.connect(str(self.socket_path))
            response = self.request("initialize", {"clientType": "airemote"})
            result = response.get("result")
            client_id = result.get("clientId") if isinstance(result, dict) else None
            if not isinstance(client_id, str) or not client_id:
                raise ValueError("Codex desktop IPC returned an unsupported handshake")
            self.client_id = client_id
            return self
        except BaseException:
            self.socket.close()
            self.socket = None
            raise

    def __exit__(self, *_):
        if self.socket is not None:
            for thread_id, owner_id in self.subscriptions:
                try:
                    self.broadcast("thread-stream-following-changed", {
                        "conversationId": thread_id, "hostId": "local", "following": False,
                    }, owner_id)
                except OSError:
                    pass
            self.socket.close()
            self.socket = None

    def _exact(self, size):
        output = bytearray()
        while len(output) < size:
            part = self.socket.recv(size - len(output))
            if not part:
                raise ConnectionError("Desktop coordination socket closed")
            output.extend(part)
        return bytes(output)

    def _write(self, value):
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("Request exceeds frame limit")
        self.socket.sendall(struct.pack("<I", len(payload)) + payload)

    def _read(self):
        size = struct.unpack("<I", self._exact(4))[0]
        if not 0 < size <= MAX_FRAME_BYTES:
            raise ValueError("Desktop returned an invalid frame length")
        value = json.loads(self._exact(size))
        if not isinstance(value, dict):
            raise ValueError("Desktop returned an invalid message")
        if value.get("type") == "client-discovery-request":
            self._write({"type": "client-discovery-response", "requestId": value["requestId"],
                         "response": {"canHandle": False}})
        elif value.get("type") == "request":
            self._write({"type": "response", "requestId": value["requestId"],
                         "resultType": "error", "error": "airemote-does-not-handle-requests"})
        elif value.get("type") == "broadcast":
            self.events.append(value)
        return value

    def request(self, method, params, target=None, version=None):
        if self.read_only and method not in READ_METHODS:
            raise PermissionError('Read-only desktop connection cannot send instructions')
        request_id = str(uuid.uuid4())
        request = {
            "type": "request", "requestId": request_id, "sourceClientId": self.client_id,
            "version": VERSIONS.get(method, 0) if version is None else version,
            "method": method, "params": params,
            "timeoutMs": int(self.timeout * 1000),
        }
        if target:
            request["targetClientId"] = target
        # Do not retry write requests after a timeout: execution may have begun.
        self._write(request)
        deadline = time.monotonic() + self.timeout + 1
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            response = self._read()
            if response.get("type") == "response" and response.get("requestId") == request_id:
                if response.get("resultType") != "success":
                    raise DesktopError(response)
                return response
        raise TimeoutError("Desktop response timed out: " + method)

    def broadcast(self, method, params, target=None):
        if self.read_only and method != 'thread-stream-following-changed':
            raise PermissionError('Read-only desktop connection cannot mutate state')
        message = {"type": "broadcast", "method": method, "params": params,
                   "sourceClientId": self.client_id, "version": VERSIONS.get(method, 0)}
        if target:
            message["targetClientIds"] = [target]
        self._write(message)

    def owner(self, thread_id):
        thread_id = str(uuid.UUID(thread_id))
        response = self.request("thread-owner-discovery", {
            "conversationId": thread_id, "hostId": "local",
        })
        owner_id = response.get("handledByClientId")
        if not isinstance(owner_id, str) or not owner_id or owner_id == self.client_id:
            raise ValueError("Codex did not identify the owner of this conversation")
        return owner_id

    def state(self, thread_id, owner_id=None):
        owner_id = owner_id or self.owner(thread_id)
        self.broadcast("thread-stream-following-changed", {
            "conversationId": thread_id, "hostId": "local", "following": True,
        }, owner_id)
        self.subscriptions.append((thread_id, owner_id))
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self.socket.settimeout(max(0.1, deadline - time.monotonic()))
            message = self._read()
            params = message.get("params", {})
            if (message.get("method") == "thread-stream-state-changed"
                    and params.get("conversationId") == thread_id
                    and params.get("change", {}).get("type") == "snapshot"):
                return {"ownerClientId": owner_id, **params["change"]}
        raise TimeoutError("No conversation snapshot received")

    def steer(self, thread_id, text, client_message_id=None, conversation=None, owner_id=None):
        owner_id = owner_id or self.owner(thread_id)
        if conversation is None:
            conversation = self.state(thread_id, owner_id)["conversationState"]
        message_id = client_message_id or str(uuid.uuid4())
        cwd = conversation.get("cwd")
        return self.request("thread-follower-steer-turn", {
            "conversationId": thread_id,
            "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
            "attachments": [],
            "restoreMessage": {
                "id": message_id, "text": text, "cwd": cwd,
                "context": {
                    "workspaceRoots": [cwd] if cwd else [],
                    "collaborationMode": conversation.get("latestCollaborationMode"),
                    "turnTrigger": "user_message",
                },
            },
        }, owner_id)

    def send(self, thread_id, text, client_message_id=None):
        owner_id = self.owner(thread_id)
        state = self.state(thread_id, owner_id)["conversationState"]
        if state.get("threadRuntimeStatus", {}).get("type") == "active":
            return self.steer(thread_id, text, client_message_id, state, owner_id)
        message_id = client_message_id or str(uuid.uuid4())
        cwd = state.get("cwd")
        restore = {"id": message_id, "text": text, "cwd": cwd,
                   "context": {"workspaceRoots": [cwd] if cwd else [],
                               "collaborationMode": state.get("latestCollaborationMode"),
                               "turnTrigger": "user_message"}}
        request = {"threadId": thread_id, "clientUserMessageId": message_id,
                   "input": [{"type": "text", "text": text, "text_elements": []}],
                   "turnTrigger": "user_message"}
        if state.get("latestCollaborationMode"):
            request["collaborationMode"] = state["latestCollaborationMode"]
        return self.request("thread-follower-start-turn", {
            "conversationId": thread_id,
            "turnStart": {"request": request, "context": {
                "restoreMessage": restore, "inheritThreadSettings": True,
                "useAppServerPermissionDefault": True,
            }},
        }, owner_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["owner", "state", "steer"])
    parser.add_argument("--thread", required=True)
    parser.add_argument("--message-file")
    args = parser.parse_args()
    with DesktopIPC() as desktop:
        if args.command == "owner":
            result = {"clientId": desktop.client_id, "ownerClientId": desktop.owner(args.thread)}
        elif args.command == "state":
            result = desktop.state(args.thread)
        else:
            if not args.message_file:
                parser.error("steer requires --message-file")
            result = desktop.steer(args.thread, Path(args.message_file).read_text())
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
