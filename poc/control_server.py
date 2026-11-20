#!/usr/bin/env python3
"""Authenticated local connector: original desktop writes, native queue, new chats."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing, contextmanager, nullcontext
from datetime import datetime, timezone
import hashlib
import hmac
import fcntl
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from urllib.parse import quote, urlsplit, parse_qs

from app_server_rpc import AppServerRPC, NativeQueue, RPCError, find_codex_installation
from claude_adapter import ClaudeAdapter, ClaudeError
from codex_probe import History, ProbeHandler
from desktop_ipc import DesktopError, DesktopIPC
from deepseek_adapter import DeepSeekAdapter, DeepSeekError
from project_catalog import ProjectCatalog
from goal_transport import START_CHANNEL, GoalOwnerClient, GoalUnavailable, GoalRPCError, checked_goal, goal_revision, validate_goal_rpc


THREAD_PATTERN = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
MAX_BODY = 256 * 1024


def now():
    return datetime.now(timezone.utc).isoformat()


def desktop_model_settings(state):
    """Return display metadata only, from the same live owner snapshot as status."""
    collaboration = state.get('latestCollaborationMode') or {}
    collaboration = collaboration if isinstance(collaboration, dict) else {}
    settings = collaboration.get('settings') or {}
    settings = settings if isinstance(settings, dict) else {}
    permissions = state.get('currentPermissions') or {}
    permissions = permissions if isinstance(permissions, dict) else {}
    sandbox = permissions.get('sandboxPolicy') or {}
    profile = permissions.get('activePermissionProfile') or {}
    def text(value): return value[:160] if isinstance(value, str) and value else None
    return {
        'model': text(settings.get('model')) or text(state.get('latestModel')),
        'reasoningEffort': text(settings.get('reasoning_effort')) or text(state.get('latestReasoningEffort')),
        'collaborationMode': text(collaboration.get('mode')),
        'permissionMode': text(sandbox.get('type')) if isinstance(sandbox, dict) else None,
        'permissionProfile': text(profile.get('id')) if isinstance(profile, dict) else None,
        'approvalPolicy': text(permissions.get('approvalPolicy')),
        'source': 'original-desktop-owner',
    }


class Conflict(RuntimeError):
    pass


class Journal:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.directory.chmod(0o700)
        self.path = self.directory / "connector.sqlite"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, digest TEXT, kind TEXT, payload TEXT, status TEXT, result TEXT, created_at TEXT, updated_at TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS created_threads(id TEXT PRIMARY KEY, cwd TEXT, title TEXT, created_at TEXT)")
            # A lost response after a process exit is an unknown outcome, not
            # permission to execute the same command a second time.
            db.execute("UPDATE operations SET status='outcome_unknown' WHERE status='in_flight'")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def begin(self, key, kind, payload, *, connection=None):
        digest = hashlib.sha256(json.dumps({"kind": kind, "payload": payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        with (self.connect() if connection is None else nullcontext(connection)) as db:
            if connection is None:
                db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM operations WHERE request_key=?", (key,)).fetchone()
            if old:
                if old["digest"] != digest:
                    raise Conflict("Idempotency-Key was already used for different input")
                return dict(old), False
            operation_id = str(uuid.uuid4())
            timestamp = now()
            db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?)", (
                operation_id, key, digest, kind, json.dumps(payload, ensure_ascii=False),
                "in_flight", None, timestamp, timestamp,
            ))
            return dict(db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()), True

    def finish(self, operation_id, status, result, *, connection=None):
        with (self.connect() if connection is None else nullcontext(connection)) as db:
            db.execute("UPDATE operations SET status=?,result=?,updated_at=? WHERE id=?", (
                status, json.dumps(result, ensure_ascii=False), now(), operation_id,
            ))

    def get(self, operation_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
        if row is None:
            raise LookupError("Unknown operation")
        return dict(row)

    def created(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM created_threads ORDER BY created_at")]

    def remember_thread(self, thread_id, cwd, title):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO created_threads VALUES(?,?,?,?)", (thread_id, cwd, title, now()))


class Controller:
    def __init__(self, threads, artifact_roots, workspace_roots, directory, rpc=None, desktop_factory=DesktopIPC, discover_desktop_projects=False, goal_client=None, desktop_channel=None):
        self.initial_threads = {str(uuid.UUID(t)) for t in threads}
        self.artifact_roots = [Path(p).resolve(strict=True) for p in artifact_roots]
        self.workspace_roots = [Path(p).resolve(strict=True) for p in workspace_roots]
        self.discover_desktop_projects = discover_desktop_projects
        self.catalog = ProjectCatalog(self.workspace_roots, include_desktop=discover_desktop_projects)
        state_directory = Path(directory).resolve()
        state_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        state_directory.chmod(0o700)
        self.instance_lock = open(state_directory / "connector.lock", "a+")
        os.chmod(self.instance_lock.name, 0o600)
        try:
            fcntl.flock(self.instance_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            raise Conflict("Another connector already uses this state directory")
        self.journal = Journal(state_directory)
        self.deepseek = DeepSeekAdapter(state_directory.parent / 'deepseek-connection.json',
            Journal(state_directory / 'providers/deepseek'), self.artifact_roots)
        self.claude = ClaudeAdapter(Journal(state_directory / 'providers/claude'),
            self.artifact_roots, self.workspace_roots, discover_desktop=discover_desktop_projects)
        self.desktop_factory = desktop_factory
        self.desktop_channel = desktop_channel or GoalOwnerClient()
        self.goal_client = goal_client or self.desktop_channel
        self.desktop_reader = (lambda: DesktopIPC(timeout=2, read_only=True)) if desktop_factory is DesktopIPC else desktop_factory
        self.lock = threading.RLock()
        self.owns_rpc = rpc is None
        try:
            self.rpc = rpc or AppServerRPC().__enter__()
        except BaseException:
            self.instance_lock.close()
            raise
        self.queue = NativeQueue(self.rpc)
        self.recoverable_created = {r["id"] for r in self.journal.created()}

    def close(self):
        self.claude.close()
        if self.owns_rpc:
            self.rpc.close()
        if not self.instance_lock.closed:
            self.instance_lock.close()

    def allowed(self, thread_id):
        thread_id = str(uuid.UUID(thread_id))
        created = {r["id"] for r in self.journal.created()}
        if thread_id not in self.initial_threads | created:
            if self.discover_desktop_projects and self.catalog.project_for_thread(thread_id) is not None:
                return thread_id
            raise PermissionError("Thread has not been authorized for this connector")
        return thread_id

    def history(self, thread_id):
        return History(self.allowed(thread_id), self.artifact_roots)

    def codex_descriptor(self):
        try:
            _, info, _ = find_codex_installation()
        except FileNotFoundError:
            info = {}
        connected = self.desktop_channel.connected()
        if not connected:
            try:
                with self.desktop_reader():
                    connected = True
            except (OSError, RuntimeError, ValueError):
                pass
        readable = connected or any((Path.home() / '.codex').glob('thread_history_*.sqlite'))
        features = ['projects', 'messages', 'runtime-status', 'artifacts', 'queue', 'goals']
        if connected:
            features += ['send', 'create', 'queue-edit', 'queue-delete', 'queue-send', 'queue-reorder']
        return {'id': 'codex', 'name': 'Codex', 'clientName': 'Codex', 'installed': bool(info) or connected,
                'version': info.get('CFBundleShortVersionString'), 'connected': connected, 'readable': readable,
                'status': 'connected' if connected else 'unavailable',
                'statusLabel': '已连接' if connected else '桌面通道未连接' if readable else '未连接',
                'features': features, 'message': '' if connected else START_CHANNEL}

    def session(self, thread_id):
        history = self.history(thread_id)
        result = history.snapshot()
        result['modelSettings'] = None
        native_state = None
        if self.discover_desktop_projects:
            result['projectId'] = self.catalog.project_for_thread(thread_id)
        try:
            owner, _ = self.desktop_channel.find_owner(thread_id, conversation=True)
            if owner is not None:
                current = self.desktop_channel.conversation(thread_id, owner)
                result.update(runtimeStatus=current['status'], runtimeSource='original desktop app-server',
                              runtimeStatusNote='Observed directly from the existing desktop owner.',
                              modelSettings=desktop_model_settings({'latestModel': current.get('model'),
                                  'latestReasoningEffort': current.get('reasoningEffort')}))
            else:
                with self.desktop_reader() as desktop:
                    snapshot = desktop.state(thread_id)
                    native_state = snapshot['conversationState']
                result.update(runtimeStatus=native_state.get('threadRuntimeStatus'),
                              runtimeSource='original desktop IPC owner',
                              runtimeStatusNote='Observed directly from the existing desktop owner.',
                              ownerClientId=snapshot['ownerClientId'], modelSettings=desktop_model_settings(native_state))
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            result['runtimeStatusNote'] = 'Live desktop status unavailable: ' + type(error).__name__
            if thread_id in self.rpc.created_threads or thread_id in self.recoverable_created:
                thread = self.rpc.request("thread/read", {"threadId": thread_id})["thread"]
                result.update({"runtimeStatus": thread.get("status"), "runtimeSource": "connector-created Codex thread"})
        result['goalState'] = self.goal_snapshot(thread_id, native_state)
        return result

    def goal_snapshot(self, thread_id, native_state=None):
        thread_id = self.allowed(thread_id)
        message = ''
        try:
            result = self.goal_client.request('thread/goal/get', {'threadId': thread_id})
            goal = checked_goal(result['goal'], thread_id)
            return {'goal': goal, 'readable': True, 'writable': True, 'revision': goal_revision(goal),
                    'source': 'original-desktop-owner', 'message': ''}
        except (GoalUnavailable, GoalRPCError, OSError, ValueError, KeyError) as error:
            message = str(error)
        if native_state is not None and 'threadGoal' in native_state:
            try:
                goal = checked_goal(native_state['threadGoal'], thread_id)
                return {'goal': goal, 'readable': True, 'writable': False, 'revision': goal_revision(goal),
                        'source': 'original-desktop-snapshot', 'message': message}
            except ValueError:
                pass
        try:
            # Read persisted metadata only. Never load/resume an existing chat here.
            goal = checked_goal(self.rpc.request('thread/goal/get', {'threadId': thread_id}, timeout=3)['goal'], thread_id)
            return {'goal': goal, 'readable': True, 'writable': False, 'revision': goal_revision(goal),
                    'source': 'persisted-goal', 'message': message}
        except (RuntimeError, OSError, KeyError, TypeError, ValueError):
            return {'goal': None, 'readable': False, 'writable': False, 'revision': None,
                    'source': 'unavailable', 'message': message or '目标状态暂未同步，请稍后刷新。'}

    def _goal_apply(self, kind, payload):
        if set(payload) - {'threadId', 'revision', 'objective', 'status', 'tokenBudget'}:
            raise ValueError('Unsupported goal changes')
        thread_id = self.allowed(payload['threadId'])
        if not isinstance(payload.get('revision'), str):
            raise ValueError('Refresh the goal before making changes')
        owner = self.goal_client.owner(thread_id)
        current = checked_goal(self.goal_client.request('thread/goal/get', {'threadId': thread_id}, owner=owner)['goal'], thread_id)
        if payload['revision'] != goal_revision(current):
            raise Conflict('目标已在其他地方更新，请刷新后再操作。')
        if kind == 'goal_create' and current is not None:
            raise Conflict('此对话已经有目标，请修改当前目标。')
        if kind != 'goal_create' and current is None:
            raise Conflict('当前目标已关闭，请刷新后再操作。')
        if kind == 'goal_clear':
            if set(payload) != {'threadId', 'revision'}:
                raise ValueError('Closing a goal only accepts its current revision')
            method, params = 'thread/goal/clear', {'threadId': thread_id, 'origin': 'user'}
        else:
            params = {key: payload[key] for key in ('objective', 'status', 'tokenBudget') if key in payload}
            if kind == 'goal_create':
                if 'objective' not in params:
                    raise ValueError('Goal objective is required')
                params.setdefault('status', 'active')
            if 'status' in params and params['status'] not in {'active', 'paused'}:
                raise ValueError('Use active or paused, or close the goal')
            method, params = 'thread/goal/set', dict(params, threadId=thread_id, origin='user')
        validate_goal_rpc(method, params)
        acknowledged = self.goal_client.request(method, params, owner=owner)
        # Read back from exactly the same process; a queued relay job isn't success.
        observed = checked_goal(self.goal_client.request('thread/goal/get', {'threadId': thread_id}, owner=owner)['goal'], thread_id)
        confirmed = observed is None if kind == 'goal_clear' else goal_revision(acknowledged['goal']) == goal_revision(observed)
        return {'threadId': thread_id, 'goal': observed, 'revision': goal_revision(observed),
                'transport': 'original-desktop-stdio-owner', 'acknowledged': True,
                'nativeConfirmed': confirmed, 'confirmation': 'observed' if confirmed else 'changed-after-ack'}

    def receipt(self, row):
        payload = json.loads(row["payload"])
        output = json.loads(row["result"]) if row["result"] else None
        result = {"operationId": row["id"], "kind": row["kind"], "requestStatus": row["status"],
                  "createdAt": row["created_at"], "updatedAt": row["updated_at"], "result": output}
        if not output or row["kind"] not in {"send", "queue_send", "queue_add", "create"}:
            return result
        thread_id = output.get("threadId") or payload.get("threadId")
        client_id = output.get("clientUserMessageId")
        if not thread_id or not client_id:
            return result
        try:
            history = self.history(thread_id)
            delivery = history.delivery_receipt(client_id)
            delivery['accepted'] = True if delivery['receivedInHistory'] else row['status'] == 'applied' and row['kind'] != 'queue_add'
            result['delivery'] = delivery
        except (OSError, sqlite3.Error, LookupError):
            result["delivery"] = {"accepted": row["status"] == "applied" and row["kind"] != "queue_add", "receivedInHistory": None}
        return result

    def perform(self, key, kind, payload):
        if not key or len(key) > 200:
            raise ValueError("A unique Idempotency-Key header is required")
        with self.lock:
            row, fresh = self.journal.begin(key, kind, payload)
            if not fresh:
                if row["status"] == "failed":
                    raise Conflict("This request already failed; inspect operation " + row["id"])
                result = self.receipt(row)
                result["replayed"] = True
                return result
            try:
                result = self._apply(kind, payload)
                self.journal.finish(row["id"], "applied", result)
            except (TimeoutError, ConnectionError, BrokenPipeError) as error:
                self.journal.finish(row["id"], "outcome_unknown", {"error": str(error), "doNotBlindlyRetry": True})
                raise Conflict("Delivery outcome is unknown; inspect this operation before retrying: " + row["id"]) from error
            except Exception as error:
                self.journal.finish(row["id"], "failed", {"error": str(error)})
                raise
            result = self.receipt(self.journal.get(row["id"]))
            result["replayed"] = False
            return result

    def _continue_desktop(self, thread_id, text, client_id, owner, queue_id):
        if queue_id is not None:
            ids = [item['id'] for item in self.queue.list(thread_id)]
            if queue_id in ids[1:]:
                # Codex can start the first queued submission as soon as it loads the chat.
                self.queue.reorder(thread_id, [queue_id] + [item for item in ids if item != queue_id])
            response = (self.desktop_channel.continue_chat(thread_id, text, client_id, owner, queue_id)
                        if queue_id in ids else {'verifyQueueDelivery': True})
            if response.get('verifyQueueDelivery'):
                history = self.history(thread_id)
                deadline = time.monotonic() + 2
                while True:
                    observed = history.delivery_receipt(client_id)
                    if observed.get('receivedInHistory'):
                        response = {'turnId': observed['turnId']}
                        break
                    if time.monotonic() >= deadline:
                        if any(item['id'] == queue_id for item in self.queue.list(thread_id)):
                            raise Conflict('这条指令仍在 Codex 队列中，请刷新查看。')
                        raise TimeoutError('Codex 队列状态已变化，正等待原对话确认是否收到该指令。')
                    time.sleep(.1)
        else:
            response = self.desktop_channel.continue_chat(thread_id, text, client_id, owner)
        result = {'threadId': thread_id, 'clientUserMessageId': client_id, 'deliveryState': 'accepted',
                  'transport': 'original-desktop-stdio-owner', 'desktopReceipt': response}
        if queue_id is not None:
            result.update(queueId=queue_id, removedAfterAcceptance=True)
        return result

    def _send(self, thread_id, text, client_id, queue_id=None):
        # Prefer the original desktop owner, including for a new conversation
        # the user has subsequently opened in the desktop app.
        owner, checked = self.desktop_channel.find_owner(thread_id, conversation=True)
        if owner is not None:
            response = self.desktop_channel.send(thread_id, text, client_id, owner)
            return {"threadId": thread_id, "clientUserMessageId": client_id,
                    "deliveryState": "accepted", "transport": "original-desktop-stdio-owner", "desktopReceipt": response}
        with ExitStack() as stack:
            try:
                desktop = stack.enter_context(self.desktop_factory())
            except (FileNotFoundError, ConnectionRefusedError, ValueError):
                desktop = None
            if desktop is not None:
                try:
                    response = desktop.send(thread_id, text, client_id)
                except DesktopError as error:
                    if error.response.get("error") != "no-client-found":
                        raise
                    checked = True
                else:
                    return {"threadId": thread_id, "clientUserMessageId": client_id,
                            "deliveryState": "accepted", "transport": "original-desktop-ipc-owner", "desktopReceipt": response}
        if thread_id not in self.rpc.created_threads and thread_id not in self.recoverable_created:
            owner, available = self.desktop_channel.resume_owner(thread_id)
            if owner is not None:
                return self._continue_desktop(thread_id, text, client_id, owner, queue_id)
            if available:
                raise Conflict('当前连接的 Codex 未找到这条已保存的对话，请确认桌面账号和设备。')
            if self.desktop_channel.connected():
                raise Conflict('请在 Mac 的 AIRemote 中通过连接通道重新启动 Codex，再从手机发送。')
            if checked:
                raise Conflict('请先在 Mac 的 Codex 中打开这条已有对话。')
        if not checked:
            raise Conflict(START_CHANNEL)
        # Only connector-created conversations can run here; the original
        # desktop conversation can never be resumed in a second process.
        if thread_id not in self.rpc.created_threads:
            self.rpc.created_threads.add(thread_id)
            self.rpc.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        state = self.rpc.request("thread/read", {"threadId": thread_id})["thread"]
        params = {"threadId": thread_id, "clientUserMessageId": client_id,
                  "input": [{"type": "text", "text": text, "text_elements": []}]}
        if state.get("status", {}).get("type") == "active":
            turns = self.rpc.request("thread/turns/list", {"threadId": thread_id, "limit": 1})["data"]
            if not turns:
                raise Conflict("Active thread has no visible turn")
            params["expectedTurnId"] = turns[0]["id"]
            response = self.rpc.request("turn/steer", params)
        else:
            response = self.rpc.request("turn/start", params)
        return {"threadId": thread_id, "clientUserMessageId": client_id,
                "deliveryState": "accepted", "transport": "connector-created-codex-thread", "desktopReceipt": response}

    def _apply(self, kind, payload):
        if kind == "create":
            if not isinstance(payload.get("text", ""), str) or not isinstance(payload.get("title", ""), str):
                raise ValueError("Title and initial message must be text")
            cwd = Path(payload["cwd"]).resolve(strict=True)
            roots = list(self.workspace_roots)
            if self.discover_desktop_projects:
                roots.extend(Path(root) for project in self.catalog.projects() for root in project['roots'])
            if not cwd.is_dir() or not any(cwd.is_relative_to(r) for r in roots):
                raise PermissionError("New conversation must use an authorized workspace")
            title = payload.get("title", "AIRemote 新对话")
            thread = self.rpc.request("thread/start", {"cwd": str(cwd), "threadSource": "user", "serviceName": "airemote"})["thread"]
            thread_id = thread["id"]
            self.journal.remember_thread(thread_id, str(cwd), title)
            self.rpc.request("thread/name/set", {"threadId": thread_id, "name": title})
            result = {"threadId": thread_id, "title": title, "cwd": str(cwd), "transport": "codex-app-server-thread-start", "isNewThread": True}
            if payload.get("text"):
                client_id = str(uuid.uuid4())
                started = self.rpc.request("turn/start", {"threadId": thread_id, "clientUserMessageId": client_id,
                    "input": [{"type": "text", "text": payload["text"], "text_elements": []}]})
                result.update({"clientUserMessageId": client_id, "turnId": started["turn"]["id"], "deliveryState": "accepted"})
            return result
        thread_id = self.allowed(payload["threadId"])
        if kind in {'goal_create', 'goal_update', 'goal_clear'}:
            return self._goal_apply(kind, payload)
        if kind == "send":
            text = payload["text"]
            if not isinstance(text, str) or not text.strip():
                raise ValueError("Message text must not be empty")
            return self._send(thread_id, text, str(uuid.uuid4()))
        if kind == "queue_add":
            if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                raise ValueError("Queued message text must not be empty")
            client_id = str(uuid.uuid4())
            return {"threadId": thread_id, "deliveryState": "queued", "clientUserMessageId": client_id,
                    "queueBehavior": "Codex may automatically execute native queued instructions, including immediately when idle.",
                    "item": self.queue.add(thread_id, payload["text"], client_id)}
        items = self.queue.list(thread_id)
        if kind == "queue_reorder":
            ids = payload["ids"]
            if len(ids) != len(set(ids)) or set(ids) != {i["id"] for i in items}:
                raise Conflict("Reorder must include each current queue item exactly once")
            self.queue.reorder(thread_id, ids)
            return {"threadId": thread_id, "items": self.queue.list(thread_id)}
        if kind == "queue_clear":
            ids = payload.get("ids")
            if ids is not None and not set(ids).issubset({i["id"] for i in items}):
                raise Conflict("A selected queue item no longer exists")
            return {"threadId": thread_id, "deletedIds": self.queue.clear(thread_id, ids)}
        item = next((i for i in items if i["id"] == payload.get("itemId")), None)
        if item is None:
            raise LookupError("Queue item no longer exists")
        if kind == "queue_update":
            if not isinstance(payload.get("text"), str) or not payload["text"].strip():
                raise ValueError("Queued message text must not be empty")
            return {"threadId": thread_id, "item": self.queue.update(thread_id, item["id"], payload["text"])}
        if kind == "queue_delete":
            return {"threadId": thread_id, "deleted": self.queue.delete(thread_id, item["id"])}
        if kind == "queue_send":
            if any(p.get("type") != "text" for p in item["input"]):
                raise ValueError("This PoC sends text-only queued instructions")
            text = "\n".join(p["text"] for p in item["input"])
            result = self._send(thread_id, text, item["clientUserMessageId"], item["id"])
            if "queueId" not in result:
                result.update({"queueId": item["id"], "removedAfterAcceptance": self.queue.delete(thread_id, item["id"])})
            return result
        raise ValueError("Unknown operation")


class ControlServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, controller, token):
        if len(token) < 24:
            raise ValueError("Use a random bearer token with at least 24 characters")
        self.controller = controller
        self.token = token
        self.history = controller.history(next(iter(controller.initial_threads)))
        super().__init__(("127.0.0.1", port), ControlHandler)


class ControlHandler(ProbeHandler):
    server_version = "AIRemote/0.4"

    def authorized(self):
        supplied = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(supplied, ("Bearer " + self.server.token).encode()):
            self.respond(401, {"error": "Authentication required"})
            return False
        return True

    def body(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ValueError("Invalid or oversized request body")
        value = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def failure(self, error):
        if isinstance(error, PermissionError):
            status = 403
        elif isinstance(error, LookupError):
            status = 404
        elif isinstance(error, (Conflict, GoalUnavailable, ClaudeError)):
            status = 409
        elif isinstance(error, (DesktopError, RPCError, DeepSeekError, GoalRPCError, ConnectionError, TimeoutError, OSError)):
            status = 503
        else:
            status = 400
        self.respond(status, {"error": str(error)})

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {"/api/session", "/api/messages", "/api/artifacts"} or path.startswith("/api/artifacts/"):
            return super().do_GET()
        if not self.authorized():
            return
        controller = self.server.controller
        try:
            if path == '/api/health':
                healthy = not getattr(controller.rpc, 'closed', False)
                self.respond(200 if healthy else 503, {'ok': healthy, 'version': '0.5.0'})
            elif path == '/api/providers':
                self.respond(200, {'providers': [
                    controller.codex_descriptor(),
                    controller.deepseek.descriptor(), controller.claude.descriptor()]})
            elif path.startswith('/api/providers/deepseek/'):
                return self.provider_get(controller.deepseek, path.removeprefix('/api/providers/deepseek'), 'session-')
            elif path.startswith('/api/providers/claude/'):
                return self.provider_get(controller.claude, path.removeprefix('/api/providers/claude'), '')
            elif path == "/api/capabilities":
                workspaces = [str(p) for p in controller.workspace_roots]
                if controller.discover_desktop_projects:
                    workspaces = list(dict.fromkeys(workspaces + [root for project in controller.catalog.projects() for root in project['roots']]))
                self.respond(200, {"provider": "codex", "hostPlatform": "macOS",
                    "workspaces": workspaces,
                    "artifactMaxBytes": 64 * 1024 * 1024, "messagesDefaultLimit": 200,
                    "features": ["existing-thread-write", "runtime-status", "messages", "artifacts", "native-queue", "create-thread", "projects", "cursor-history"]})
            elif path == "/api/projects":
                self.respond(200, {'projects': controller.catalog.projects(), 'messagePageSize': 40})
            elif re.fullmatch(r'/api/projects/(' + THREAD_PATTERN + r')/threads', path):
                project_id = path.split('/')[3]
                allowed = None if controller.discover_desktop_projects else controller.initial_threads | {r['id'] for r in controller.journal.created()}
                threads = controller.catalog.threads(project_id, allowed)
                self.respond(200, {'projectId': project_id, 'threads': controller.catalog.attach_turn_status(threads)})
            elif path == "/api/threads":
                ids = sorted(controller.initial_threads | {r["id"] for r in controller.journal.created()})
                with ThreadPoolExecutor(max_workers=min(4, max(1, len(ids)))) as pool:
                    threads = list(pool.map(controller.session, ids))
                self.respond(200, {"threads": threads})
            elif re.fullmatch(r"/api/operations/" + THREAD_PATTERN, path):
                self.respond(200, controller.receipt(controller.journal.get(path.split("/")[3])))
            else:
                download = re.fullmatch(r"/api/threads/(" + THREAD_PATTERN + r")/artifacts/([0-9a-f]{24})/download", path)
                if download:
                    artifact, content = controller.history(download.group(1)).read_artifact(download.group(2))
                    self.respond(200, content, mimetypes.guess_type(artifact["name"])[0] or "application/octet-stream", {
                        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(artifact["name"], safe=""),
                        "ETag": '"' + hashlib.sha256(content).hexdigest() + '"',
                    })
                    return
                match = re.fullmatch(r"/api/threads/(" + THREAD_PATTERN + r")(?:/(queue|messages|artifacts|goal))?", path)
                if not match:
                    self.respond(404, {"error": "Unknown endpoint"})
                    return
                thread_id = controller.allowed(match.group(1))
                resource = match.group(2)
                if resource == 'goal':
                    self.respond(200, controller.goal_snapshot(thread_id))
                elif resource == "queue":
                    self.respond(200, {"threadId": thread_id, "items": controller.queue.list(thread_id)})
                elif resource == "messages":
                    query = parse_qs(urlsplit(self.path).query)
                    limit = int(query.get("limit", [40])[0])
                    self.respond(200, controller.history(thread_id).message_page(limit, query.get('before', [None])[0]))
                elif resource == "artifacts":
                    self.respond(200, {"artifacts": controller.history(thread_id).artifacts()})
                else:
                    self.respond(200, controller.session(thread_id))
        except Exception as error:
            self.failure(error)

    def provider_get(self, adapter, path, prefix):
        if path == '/projects':
            return self.respond(200, {'projects': adapter.projects()})
        match = re.fullmatch(r'/projects/(' + THREAD_PATTERN + r')/threads', path)
        if match:
            return self.respond(200, {'threads': adapter.threads(match.group(1))})
        match = re.fullmatch(r'/operations/(' + THREAD_PATTERN + ')', path)
        if match:
            return self.respond(200, adapter.receipt(adapter.journal.get(match.group(1))))
        resources = 'messages|artifacts|queue' if prefix else 'messages|artifacts|queue|goal'
        match = re.fullmatch(r'/threads/(' + prefix + THREAD_PATTERN + r')(?:/(' + resources + r'))?', path)
        download = re.fullmatch(r'/threads/(' + prefix + THREAD_PATTERN + r')/artifacts/([0-9a-f]{24})/download', path)
        if download:
            tid, artifact_id = download.groups()
            item, content = adapter.read_artifact(tid, artifact_id)
            return self.respond(200, content, mimetypes.guess_type(item['name'])[0] or 'application/octet-stream', {
                'Content-Disposition': "attachment; filename*=UTF-8''" + quote(item['name'], safe=''),
                'ETag': '"' + hashlib.sha256(content).hexdigest() + '"'})
        if not match:
            return self.respond(404, {'error': 'Unknown provider endpoint'})
        tid, resource = match.groups()
        if resource == 'messages':
            query = parse_qs(urlsplit(self.path).query)
            return self.respond(200, adapter.message_page(tid, int(query.get('limit', [40])[0]), query.get('before', [None])[0]))
        if resource == 'queue':
            return self.respond(200, {'items': adapter.queue(tid)})
        if resource == 'goal':
            return self.respond(200, adapter.goal(tid))
        if resource == 'artifacts':
            return self.respond(200, {'artifacts': adapter.artifacts(tid)})
        return self.respond(200, adapter.session(tid))

    def deepseek_mutate(self, path, body):
        if path == '/threads' and self.command == 'POST':
            kind, payload = 'create', body
        else:
            match = re.fullmatch(r'/threads/(session-' + THREAD_PATTERN + r')/(messages|queue)(?:/((?:msg-)?' + THREAD_PATTERN + r'))?(?:/(send))?', path)
            if not match:
                return self.respond(404, {'error': 'Unknown DeepSeek write endpoint'})
            tid, resource, item_id, send = match.groups()
            payload = dict(body, threadId=tid)
            if resource == 'messages' and self.command == 'POST' and not item_id:
                kind = 'send'
            elif resource == 'queue' and not item_id and self.command in {'POST', 'DELETE'}:
                kind = 'queue_add' if self.command == 'POST' else 'queue_clear'
            elif resource == 'queue' and item_id:
                kind = 'queue_send' if send and self.command == 'POST' else 'queue_delete' if self.command == 'DELETE' else 'queue_update' if self.command == 'PATCH' else None
                payload['itemId'] = item_id
            else:
                kind = None
            if kind is None:
                raise ValueError('Unsupported DeepSeek queue operation')
        result = self.server.controller.deepseek.perform(self.headers.get('Idempotency-Key'), kind, payload)
        return self.respond(202 if result['requestStatus'] in {'in_flight', 'outcome_unknown'} else 200, result)

    def claude_mutate(self, path, body):
        if path == '/threads' and self.command == 'POST':
            kind, payload = 'create', body
        else:
            match = re.fullmatch(r'/threads/(' + THREAD_PATTERN + r')/(messages|queue|settings|stop|goal)(?:/(' +
                                 THREAD_PATTERN + r'|order))?(?:/(send))?', path)
            if not match:
                return self.respond(404, {'error': 'Unknown Claude Code write endpoint'})
            tid, resource, item, priority = match.groups()
            action = (self.command, resource)
            commands = {('POST', 'messages'): 'send', ('PATCH', 'settings'): 'settings', ('POST', 'stop'): 'stop',
                        ('POST', 'goal'): 'goal_set', ('PATCH', 'goal'): 'goal_set', ('DELETE', 'goal'): 'goal_clear',
                        ('POST', 'queue'): 'queue_add', ('DELETE', 'queue'): 'queue_clear'}
            payload = dict(body, threadId=tid)
            if not item:
                kind = commands.get(action)
                if action == ('PATCH', 'goal') and 'objective' not in body:
                    kind = {'paused': 'goal_pause', 'active': 'goal_resume'}.get(body.get('status'))
            elif resource == 'queue' and item == 'order' and self.command == 'PUT' and not priority:
                kind = 'queue_reorder'
            elif resource == 'queue' and item != 'order':
                kind = {('POST', 'send'): 'queue_send', ('PATCH', None): 'queue_update',
                        ('DELETE', None): 'queue_delete'}.get((self.command, priority))
                payload['itemId'] = item
            else:
                kind = None
            if not kind:
                raise ValueError('Unsupported Claude Code operation')
        result = self.server.controller.claude.perform(self.headers.get('Idempotency-Key'), kind, payload)
        return self.respond(202 if result['requestStatus'] in {'queued', 'in_flight', 'outcome_unknown'} else 200, result)

    def mutate(self):
        if not self.authorized():
            return
        path = urlsplit(self.path).path
        try:
            body = self.body()
            if path.startswith('/api/providers/claude/'):
                return self.claude_mutate(path.removeprefix('/api/providers/claude'), body)
            if path.startswith('/api/providers/deepseek/'):
                return self.deepseek_mutate(path[len('/api/providers/deepseek'):], body)
            if path == "/api/threads" and self.command == "POST":
                kind, payload = "create", body
            else:
                match = re.fullmatch(r"/api/threads/(" + THREAD_PATTERN + r")/(messages|queue|goal)(?:/([0-9a-f-]+|order))?(?:/(send))?", path)
                if not match:
                    self.respond(404, {"error": "Unknown endpoint"})
                    return
                thread_id = self.server.controller.allowed(match.group(1))
                resource, item, send = match.group(2), match.group(3), match.group(4)
                payload = {**body, "threadId": thread_id}
                if resource == 'goal' and not item and not send and self.command in {'POST', 'PATCH', 'DELETE'}:
                    kind = {'POST': 'goal_create', 'PATCH': 'goal_update', 'DELETE': 'goal_clear'}[self.command]
                elif resource == "messages" and not item and self.command == "POST":
                    kind = "send"
                elif resource == "queue":
                    if not item and self.command == "POST":
                        kind = "queue_add"
                    elif not item and self.command == "DELETE":
                        kind = "queue_clear"
                    elif item == "order" and self.command == "PUT":
                        kind = "queue_reorder"
                    elif item and send == "send" and self.command == "POST":
                        kind = "queue_send"
                        payload["itemId"] = item
                    elif item and not send and self.command in {"PATCH", "DELETE"}:
                        kind = "queue_update" if self.command == "PATCH" else "queue_delete"
                        payload["itemId"] = item
                    else:
                        raise ValueError("Unsupported queue operation")
                else:
                    raise ValueError("Unsupported operation")
            result = self.server.controller.perform(self.headers.get("Idempotency-Key"), kind, payload)
            self.respond(202 if result["requestStatus"] in {"in_flight", "outcome_unknown"} else 200, result)
        except Exception as error:
            self.failure(error)

    do_POST = mutate
    do_PATCH = mutate
    do_DELETE = mutate
    do_PUT = mutate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", action="append", default=[])
    parser.add_argument("--artifact-root", action="append", required=True)
    parser.add_argument("--workspace-root", action="append", required=True)
    parser.add_argument("--state-dir", default=str(Path(__file__).resolve().parents[1] / ".local"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--token-file")
    parser.add_argument('--discover-desktop-projects', action='store_true', help='Discover local projects from desktop clients')
    args = parser.parse_args()
    if not args.thread and not args.discover_desktop_projects:
        parser.error('Provide --thread or explicitly enable --discover-desktop-projects')
    controller = Controller(args.thread, args.artifact_root, args.workspace_root, args.state_dir,
                            discover_desktop_projects=args.discover_desktop_projects)
    try:
        if args.token_file:
            token_path = Path(args.token_file)
            info = token_path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise PermissionError("Token file must be private and belong to this user")
            token = token_path.read_text().strip()
        else:
            token = os.environ.get("AIREMOTE_POC_TOKEN", "")
        server = ControlServer(args.port, controller, token)
        print(json.dumps({"listening": "http://127.0.0.1:%s" % server.server_port,
                          "authorizedThreads": sorted(controller.initial_threads), "mode": "read-write",
                          "pid": os.getpid()}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
    finally:
        controller.close()


if __name__ == "__main__":
    main()
