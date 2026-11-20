#!/usr/bin/env python3
"""Opt-in access to conversations in the desktop's original stdio app-server.

Launched only via Codex's CODEX_CLI_PATH override. Native initialization, all
notifications and approvals pass through unchanged. This never resumes a chat,
initializes another client, or opens a Codex private control socket.
"""
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid

GOAL_METHODS = {'thread/goal/get', 'thread/goal/set', 'thread/goal/clear'}
DESKTOP_METHODS = {'desktop/read', 'desktop/send'}
STATUSES = {'active', 'paused', 'blocked', 'usageLimited', 'budgetLimited', 'complete'}
MAX_PACKET = 256 * 1024
DEFAULT_DIRECTORY = Path.home() / '.airemote/goal-channel'
OPEN_CHAT = '请在 Mac 的 AIRemote 中通过连接通道启动 Codex，再打开这个对话。'


class GoalUnavailable(RuntimeError):
    pass


class GoalRPCError(RuntimeError):
    pass


def private_path(path, directory=False):
    path = Path(path)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise PermissionError('Goal channel files must be private and owned by this user')
    if directory and not stat.S_ISDIR(info.st_mode):
        raise PermissionError('Invalid goal channel directory')
    return path


def goal_revision(goal):
    # Counters change while running; they are intentionally outside the edit guard.
    value = None if goal is None else {key: goal.get(key) for key in
        ('threadId', 'createdAt', 'objective', 'status', 'tokenBudget')}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def checked_goal(goal, thread_id):
    if goal is None:
        return None
    if not isinstance(goal, dict) or goal.get('threadId') != thread_id:
        raise ValueError('Goal response does not belong to the selected chat')
    if not isinstance(goal.get('objective'), str) or goal.get('status') not in STATUSES:
        raise ValueError('Unsupported goal response')
    for key in ('tokensUsed', 'timeUsedSeconds', 'createdAt', 'updatedAt'):
        if type(goal.get(key)) not in (int, float) or goal[key] < 0:
            raise ValueError('Invalid goal counters')
    budget = goal.get('tokenBudget')
    if budget is not None and (type(budget) is not int or budget <= 0):
        raise ValueError('Invalid goal budget')
    return {key: goal.get(key) for key in ('threadId', 'objective', 'status', 'tokenBudget',
                                         'tokensUsed', 'timeUsedSeconds', 'createdAt', 'updatedAt')}


def checked_thread_id(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError('Invalid thread ID')
    return value


def validate_goal_rpc(method, params):
    if method not in GOAL_METHODS or not isinstance(params, dict):
        raise ValueError('Only explicit goal operations are supported')
    tid = checked_thread_id(params.get('threadId'))
    allowed = {'threadId'} if method.endswith('/get') else {'threadId', 'origin'}
    if method.endswith('/set'):
        allowed |= {'objective', 'status', 'tokenBudget'}
        if not any(key in params for key in ('objective', 'status', 'tokenBudget')):
            raise ValueError('No goal changes supplied')
        if 'objective' in params and (not isinstance(params['objective'], str) or not params['objective'].strip() or len(params['objective']) > 32000):
            raise ValueError('Goal objective must contain 1–32000 characters')
        if 'status' in params and params['status'] not in STATUSES:
            raise ValueError('Unknown goal status')
        budget = params.get('tokenBudget')
        if budget is not None and (type(budget) is not int or not 1 <= budget <= 9007199254740991):
            raise ValueError('Token budget must be a positive safe integer or null')
    if set(params) - allowed:
        raise ValueError('Unsupported goal parameters')
    if not method.endswith('/get') and params.get('origin') != 'user':
        raise PermissionError('Goal changes require explicit user provenance')
    return tid


def receive_packet(stream):
    line = stream.readline(MAX_PACKET + 1)
    if not line or len(line) > MAX_PACKET or not line.endswith(b'\n'):
        raise ValueError('Invalid goal channel packet')
    value = json.loads(line)
    if not isinstance(value, dict):
        raise ValueError('Goal channel packet must be an object')
    return value


class GoalOwnerClient:
    def __init__(self, directory=None):
        self.directory = Path(directory or DEFAULT_DIRECTORY)

    def _records(self):
        try:
            private_path(self.directory, True)
            config = json.loads(private_path(self.directory / 'config.json').read_text())
            token = config['token']
            if not isinstance(token, str) or len(token) < 32:
                raise ValueError('Invalid channel token')
        except FileNotFoundError:
            raise GoalUnavailable('请在 Mac 的 AIRemote 中启用 Codex 目标控制通道。')
        records = []
        for path in sorted(self.directory.glob('s*.json')):
            try:
                row = json.loads(private_path(path).read_text())
                if type(row.get('pid')) is not int or row['pid'] <= 0:
                    continue
                os.kill(row['pid'], 0)
                name = row['socket']
                if not isinstance(name, str) or Path(name).name != name or not name.startswith('s') or not name.endswith('.sock'):
                    continue
                sockpath = private_path(self.directory / name)
                if not stat.S_ISSOCK(sockpath.lstat().st_mode):
                    continue
                records.append((sockpath, token))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        return records

    @staticmethod
    def _call(record, method, params, timeout=15):
        path, token = record
        with socket.socket(socket.AF_UNIX) as connection:
            connection.settimeout(timeout)
            connection.connect(str(path))
            connection.sendall(json.dumps({'token': token, 'method': method, 'params': params}, ensure_ascii=False).encode() + b'\n')
            with connection.makefile('rb') as stream:
                response = receive_packet(stream)
        if 'error' in response:
            error = response['error']
            if error.get('code') == 'outcome_unknown':
                raise TimeoutError(error['message'])
            if error.get('code') in {'not_ready', 'not_loaded'}:
                raise GoalUnavailable(error['message'])
            raise GoalRPCError(error.get('message', 'Goal request rejected'))
        result = response.get('result')
        if not isinstance(result, dict):
            raise ConnectionError('Codex channel returned an unrecognized response')
        return result

    def connected(self):
        try:
            records = self._records()
        except (GoalUnavailable, OSError, ValueError, KeyError):
            return False
        for record in records:
            try:
                if self._call(record, 'status', {}, timeout=1).get('conversationControl') is True:
                    return True
            except (RuntimeError, OSError, ValueError, KeyError):
                continue
        return False

    def find_owner(self, thread_id, *, conversation=False):
        checked_thread_id(thread_id)
        try:
            records = self._records()
        except GoalUnavailable:
            return None, False
        except (OSError, ValueError, KeyError):
            if conversation:
                return None, False
            raise
        owners = []
        checked = False
        for record in records:
            try:
                value = self._call(record, 'owner', {'threadId': thread_id}, timeout=4)
                if conversation and value.get('conversationControl') is not True:
                    continue
                if type(value.get('loaded')) is not bool:
                    continue
                checked = True
                if value['loaded']:
                    owners.append(record)
            except (OSError, ValueError, KeyError, GoalUnavailable, GoalRPCError):
                continue
        if len(owners) > 1:
            raise GoalUnavailable('检测到多个对话持有者，请在 Mac 上确认当前对话。')
        return (owners[0] if owners else None), checked

    def owner(self, thread_id):
        owner, _ = self.find_owner(thread_id)
        if owner is None:
            raise GoalUnavailable(OPEN_CHAT)
        return owner

    def request(self, method, params, owner=None):
        tid = validate_goal_rpc(method, params)
        return self._call(owner or self.owner(tid), method, params)

    def conversation(self, thread_id, owner):
        return self._call(owner, 'desktop/read', {'threadId': thread_id})

    def send(self, thread_id, text, message_id, owner):
        return self._call(owner, 'desktop/send', {'threadId': thread_id, 'text': text,
                                                 'clientUserMessageId': message_id}, timeout=30)


class StdioGoalProxy:
    def __init__(self, command, directory, token, input_stream=None, output_stream=None):
        self.command, self.directory, self.token = command, Path(directory), token
        self.input = input_stream or sys.stdin.buffer
        self.output = output_stream or sys.stdout.buffer
        self.pending = {}
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.ready = threading.Event()
        self.closed = threading.Event()
        self.input_closed_at = None
        self.prefix = 'airemote:' + uuid.uuid4().hex + ':'
        self.native_initialize_id = None
        self.native_initialize_ok = False

    def send(self, packet):
        with self.write_lock:
            if self.closed.is_set():
                raise ConnectionError('Original Codex app-server has exited')
            self.process.stdin.write(packet)
            self.process.stdin.flush()

    def rpc(self, method, params, timeout=10):
        request_id = self.prefix + uuid.uuid4().hex
        event = threading.Event()
        slot = {'event': event}
        with self.lock:
            self.pending[request_id] = slot
        try:
            self.send(json.dumps({'id': request_id, 'method': method, 'params': params}).encode() + b'\n')
            if not event.wait(timeout):
                raise TimeoutError('Original Codex goal response was not observed; do not blindly retry')
            response = slot.get('response')
            if response is None:
                raise ConnectionError('Original Codex disconnected before confirming the request')
            if 'error' in response:
                raise GoalRPCError(str(response['error'].get('message', 'Goal RPC rejected')))
            return response.get('result')
        finally:
            with self.lock:
                # All responses in our random namespace are consumed, even late
                # responses to timed-out requests. Pending memory stays bounded.
                self.pending.pop(request_id, None)

    def native_input(self):
        try:
            for line in self.input:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict) and value.get('method') == 'initialize':
                        self.native_initialize_id = value.get('id')
                except (ValueError, TypeError):
                    pass
                self.send(line)
        except (OSError, ConnectionError):
            pass
        finally:
            self.input_closed_at = time.monotonic()
            with self.write_lock:
                try:
                    self.process.stdin.close()
                except OSError:
                    pass

    def native_output(self):
        try:
            for line in self.process.stdout:
                value = None
                try:
                    value = json.loads(line)
                except ValueError:
                    pass
                if isinstance(value, dict) and 'id' in value and 'method' not in value:
                    request_id = value['id']
                    if isinstance(request_id, str) and request_id.startswith(self.prefix):
                        with self.lock:
                            slot = self.pending.pop(request_id, None)
                            if slot:
                                slot['response'] = value
                                slot['event'].set()
                        continue
                    if (self.native_initialize_id is not None and request_id == self.native_initialize_id
                            and 'error' not in value and isinstance(value.get('result'), dict)):
                        self.native_initialize_ok = True
                self.output.write(line)
                self.output.flush()
                # Codex Desktop starts regular RPCs after initialize succeeds;
                # unlike the SDK, it does not send an initialized notification.
                # Observe the native response, forward it, then allow Goal RPCs.
                if self.native_initialize_ok:
                    self.ready.set()
        finally:
            self.closed.set()
            with self.lock:
                for slot in self.pending.values():
                    slot['event'].set()

    def loaded_threads(self, timeout=10):
        params, cursors = {}, set()
        while True:
            page = self.rpc('thread/loaded/list', params, timeout=timeout)
            ids = page.get('data') if isinstance(page, dict) else None
            if not isinstance(ids, list) or any(not isinstance(tid, str) for tid in ids):
                raise GoalUnavailable('Codex 返回了不兼容的已打开对话列表。')
            yield from ids
            cursor = page.get('nextCursor')
            if cursor is None:
                return
            if not isinstance(cursor, str) or cursor in cursors:
                raise GoalUnavailable('Codex 返回了无效的已打开对话分页。')
            cursors.add(cursor)
            params = {'cursor': cursor}

    def read_conversation(self, tid):
        result = self.rpc('thread/read', {'threadId': tid, 'includeTurns': False})
        thread = result.get('thread') if isinstance(result, dict) else None
        if not isinstance(thread, dict) or thread.get('id') != tid or not isinstance(thread.get('status'), dict):
            raise GoalUnavailable('Codex 未能确认当前对话的状态。')
        return {key: thread.get(key) for key in ('id', 'status', 'model', 'reasoningEffort', 'canAcceptDirectInput')}

    def desktop_call(self, method, params):
        fields = {'threadId'} if method == 'desktop/read' else {'threadId', 'text', 'clientUserMessageId'}
        if not isinstance(params, dict) or set(params) != fields:
            raise ValueError('Invalid desktop conversation request')
        tid = checked_thread_id(params['threadId'])
        if tid not in self.loaded_threads():
            raise GoalUnavailable(OPEN_CHAT)
        thread = self.read_conversation(tid)
        if method == 'desktop/read':
            return thread
        text = params['text']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Message text must not be empty')
        if thread.get('canAcceptDirectInput') is False:
            raise GoalUnavailable('当前 Codex 对话不接受直接输入。')
        request = {'threadId': tid, 'clientUserMessageId': checked_thread_id(params['clientUserMessageId']),
                   'input': [{'type': 'text', 'text': text, 'text_elements': []}]}
        status = thread['status'].get('type')
        if status == 'idle':
            result = self.rpc('turn/start', {**request, 'turnTrigger': 'user_message'})
            turn = result.get('turn') if isinstance(result, dict) else None
            confirmed = turn.get('id') if isinstance(turn, dict) else None
        elif status == 'active':
            turns = self.rpc('thread/turns/list', {'threadId': tid, 'limit': 1}).get('data')
            turn_id = turns[0].get('id') if isinstance(turns, list) and turns and isinstance(turns[0], dict) else None
            if not isinstance(turn_id, str) or not turn_id:
                raise GoalUnavailable('Codex 未能确认当前运行的轮次，请刷新对话后重试。')
            result = self.rpc('turn/steer', {**request, 'expectedTurnId': turn_id})
            confirmed = result.get('turnId') if isinstance(result, dict) else None
            if confirmed != turn_id:
                raise ConnectionError('Codex returned an unexpected turn acknowledgement')
        else:
            raise GoalUnavailable('Codex 未能确认可发送指令的轮次，请刷新对话后重试。')
        if not isinstance(confirmed, str) or not confirmed:
            raise ConnectionError('Codex returned an unrecognized turn acknowledgement')
        return {'turnId': confirmed}

    def handle(self, connection):
        try:
            connection.settimeout(30)
            with connection, connection.makefile('rwb') as stream:
                try:
                    packet = receive_packet(stream)
                    if not isinstance(packet.get('token'), str) or not hmac.compare_digest(packet['token'], self.token):
                        raise PermissionError('Goal channel authentication failed')
                    if set(packet) != {'token', 'method', 'params'}:
                        raise ValueError('Invalid goal channel request')
                    method, params = packet['method'], packet['params']
                    ready = self.ready.is_set() and not self.closed.is_set()
                    if method == 'status' and params == {}:
                        if ready:
                            next(self.loaded_threads(timeout=.7), None)
                        result = {'ready': ready, 'conversationControl': ready}
                    elif not ready:
                        raise GoalUnavailable('Original desktop initialization is not ready')
                    elif method in DESKTOP_METHODS:
                        result = self.desktop_call(method, params)
                    else:
                        tid = validate_goal_rpc('thread/goal/get' if method == 'owner' else method, params)
                        loaded = tid in self.loaded_threads()
                        if method == 'owner':
                            result = {'loaded': loaded, 'conversationControl': True, 'transport': 'original-desktop-stdio'}
                        elif not loaded:
                            raise GoalUnavailable(OPEN_CHAT)
                        else:
                            if method != 'thread/goal/get':
                                current = self.rpc('thread/goal/get', {'threadId': tid})
                                if not isinstance(current, dict):
                                    raise GoalUnavailable('Codex 返回了不兼容的目标状态。')
                                checked_goal(current.get('goal'), tid)
                            result = self.rpc(method, params)
                    response = {'result': result}
                except Exception as error:
                    code = 'outcome_unknown' if isinstance(error, (TimeoutError, ConnectionError)) else 'not_loaded' if isinstance(error, GoalUnavailable) else 'rejected'
                    response = {'error': {'code': code, 'message': str(error)}}
                stream.write(json.dumps(response, ensure_ascii=False).encode() + b'\n')
                stream.flush()
        except OSError:
            pass

    def run(self):
        private_path(self.directory, True)
        name = 's' + secrets.token_hex(6)
        socket_path = self.directory / (name + '.sock')
        registry = self.directory / (name + '.json')
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, bufsize=65536)
        def stop(signum, _frame):
            self.closed.set()
            if self.process.poll() is None:
                self.process.send_signal(signum)
        old_signals = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        listener = socket.socket(socket.AF_UNIX)
        try:
            listener.bind(str(socket_path)); socket_path.chmod(0o600)
            listener.listen(8); listener.settimeout(0.5)
            fd = os.open(str(registry), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'pid': os.getpid(), 'socket': socket_path.name, 'transport': 'original-desktop-stdio'}, stream)
            threading.Thread(target=self.native_input, daemon=True).start()
            threading.Thread(target=self.native_output, daemon=True).start()
            while not self.closed.is_set():
                if self.input_closed_at is not None and time.monotonic() - self.input_closed_at > 5:
                    break
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                threading.Thread(target=self.handle, args=(connection,), daemon=True).start()
            return self.process.wait(timeout=10)
        finally:
            for sig, handler in old_signals.items():
                signal.signal(sig, handler)
            self.closed.set()
            listener.close()
            registry.unlink(missing_ok=True); socket_path.unlink(missing_ok=True)
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill(); self.process.wait()


def serve_stdio(args):
    config_path = private_path(args.config)
    config = json.loads(config_path.read_text())
    command = [config['executable']] + args.arguments
    # CLI version checks and schema/daemon tooling retain their normal behavior.
    actual_server = 'app-server' in args.arguments and not any(arg in args.arguments for arg in
        ('--help', '-h', '--print-startup-config', 'help', 'generate-json-schema', 'generate-ts', 'daemon', 'proxy'))
    for index, arg in enumerate(args.arguments):
        if arg == '--listen' and args.arguments[index + 1:index + 2] != ['stdio://']:
            actual_server = False
        elif arg.startswith('--listen=') and arg != '--listen=stdio://':
            actual_server = False
    if not actual_server:
        os.execv(command[0], command)
    return StdioGoalProxy(command, config_path.parent, config['token']).run()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    arguments = parser.parse_args()
    if arguments.arguments[:1] == ['--']:
        arguments.arguments = arguments.arguments[1:]
    raise SystemExit(serve_stdio(arguments))
