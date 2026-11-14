"""Adapter for the user's existing, authenticated DeepSeek Harness Host.

The optional companion publishes the Host's normal launch URL privately. This
client exchanges it for the normal cookie, then calls the installed Host API.
It never starts a second Harness, copies chats, or reads account/API keys.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.cookiejar
import json
import mimetypes
import os
from pathlib import Path
import plistlib
import re
import threading
import uuid
from urllib.parse import unquote, urlsplit
from urllib.request import Request, build_opener, HTTPCookieProcessor, ProxyHandler


TESTED_VERSION = '0.2.2-rc.1'
APP_PATH = Path('/Applications/DeepSeek Harness.app')
MAX_ARTIFACT = 64 * 1024 * 1024


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, connection_file):
        self.path = Path(connection_file)
        self.local = threading.local()

    def connection(self):
        stat = self.path.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise PermissionError('DeepSeek connection file must be private to this user')
        value = json.loads(self.path.read_text())
        origin = urlsplit(value['origin'])
        target = urlsplit(value['authenticatedUrl'])
        if (origin.scheme != 'http' or origin.hostname != '127.0.0.1' or not origin.port
                or origin.path or origin.query or origin.fragment or origin.username
                or target.scheme != origin.scheme or target.netloc != origin.netloc
                or target.path != '/' or target.fragment or not target.query.startswith('token=')):
            raise PermissionError('DeepSeek must use its own authenticated loopback Host')
        os.kill(int(value['pid']), 0)
        if value.get('desktopVersion') != TESTED_VERSION:
            raise DeepSeekError('DeepSeek adapter requires validation for this installed version')
        if APP_PATH.exists() and plistlib.loads((APP_PATH / 'Contents/Info.plist').read_bytes()).get('CFBundleShortVersionString') != TESTED_VERSION:
            raise DeepSeekError('DeepSeek adapter requires validation for this installed version')
        return value

    def opener(self):
        cfg = self.connection()
        generation = (cfg['pid'], cfg['authenticatedUrl'])
        if getattr(self.local, 'generation', None) != generation:
            opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(http.cookiejar.CookieJar()))
            # The token and resulting cookie stay in this Mac process only.
            with opener.open(cfg['authenticatedUrl'], timeout=5) as response:
                response.read(1024)
            self.local.opener = opener
            self.local.generation = generation
        return cfg, self.local.opener

    def call(self, endpoint, request=None, parameter='request'):
        cfg, opener = self.opener()
        rpc_id = str(uuid.uuid4())
        args = {} if request is None else {parameter: request}
        body = {'type': 'client-request', 'rpcId': rpc_id, 'method': endpoint, 'payload': {'args': args}}
        req = Request(cfg['origin'] + '/api/' + endpoint, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=25) as response:
            value = json.load(response)
        if value.get('rpcId') != rpc_id or value.get('type') != 'server-response':
            raise DeepSeekError('DeepSeek returned an invalid RPC envelope')
        result = value['result']
        if not result.get('ok'):
            error = result.get('error', {})
            raise DeepSeekError(str(error.get('code', 'error')) + ': ' + str(error.get('message', 'DeepSeek request failed')))
        return result.get('value')


def content_text(parts):
    if isinstance(parts, str):
        return parts
    return '\n'.join(part.get('text', '') for part in (parts or []) if isinstance(part, dict) and part.get('type') == 'text')


class DeepSeekAdapter:
    def __init__(self, connection_file, journal, artifact_roots, home=None):
        self.client = DeepSeekClient(connection_file)
        self.journal = journal
        self.home = Path(home or Path.home() / '.dsh')
        self.artifact_roots = [Path(p).resolve() for p in artifact_roots]
        self.lock = threading.RLock()

    def descriptor(self):
        value = {'id': 'deepseek', 'name': 'DeepSeek', 'clientName': 'DeepSeek Harness',
                 'installed': APP_PATH.exists(), 'connected': False, 'readable': False,
                 'status': 'not_installed', 'statusLabel': '未安装',
                 'features': ['projects', 'messages', 'runtime-status', 'artifacts', 'send', 'create', 'queue', 'queue-edit', 'queue-delete', 'queue-send'],
                 'helpUrl': 'https://deepseek.com/download/'}
        if APP_PATH.exists():
            info = plistlib.loads((APP_PATH / 'Contents/Info.plist').read_bytes())
            value['version'] = info.get('CFBundleShortVersionString')
            value.update(status='not_running', statusLabel='未运行')
            if value['version'] != TESTED_VERSION:
                value.update(status='unsupported', statusLabel='版本待验证')
                return value
        try:
            cfg, opener = self.client.opener()
            with opener.open(cfg['origin'] + '/', timeout=2) as response:
                response.read(256)
            value.update(connected=True, readable=True, status='connected', statusLabel='已连接')
        except (OSError, ValueError, KeyError, DeepSeekError):
            pass
        return value

    def _workspaces(self):
        path = self.home / 'storages/workspace.json'
        if not path.exists():
            return []
        value = json.loads(path.read_text())
        rows = value.get('tables', {}).get('workspaces', {})
        return [dict(rows[key], id=key) for key in value.get('global', {}).get('workspaceIds', []) if key in rows]

    def projects(self):
        # The registry contains only membership/metadata; no session logs are opened.
        return [{'id': row['id'], 'name': row['title'], 'path': row['path'], 'roots': [row['path']]} for row in self._workspaces()]

    def workspace_for_thread(self, thread_id):
        for row in self._workspaces():
            if thread_id in row.get('sessionIds', []):
                return row
        raise LookupError('DeepSeek conversation is not in a native workspace')

    def threads(self, project_id):
        workspace = next((row for row in self._workspaces() if row['id'] == project_id), None)
        if workspace is None:
            raise LookupError('Unknown DeepSeek project')
        def summary(thread_id):
            result = self.client.call('session/projections', {'sessionId': thread_id}) or {}
            values = result.get('values', {})
            state = self.client.call('session/state', {'sessionId': thread_id})
            return {'threadId': thread_id, 'title': values.get('title') or '新对话', 'cwd': workspace['path'],
                    'projectId': project_id, 'turns': [],
                    'runtimeStatus': {'type': 'active' if state['running'] else 'idle', 'activeFlags': state.get('pendingInput', [])},
                    'runtimeSource': 'existing DeepSeek Harness Host', 'latestPersistedTurnStatus': None}
        ids = workspace.get('sessionIds', [])
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(summary, ids))

    def state(self, thread_id):
        self.workspace_for_thread(thread_id)
        return self.client.call('session/state', {'sessionId': thread_id})

    def session(self, thread_id):
        workspace = self.workspace_for_thread(thread_id)
        state = self.state(thread_id)
        projection = self.client.call('session/projections', {'sessionId': thread_id}) or {}
        values = projection.get('values', {})
        flags = list(state.get('pendingInput', []))
        return {'threadId': thread_id, 'title': values.get('title') or '新对话', 'cwd': workspace['path'],
                'projectId': workspace['id'], 'runtimeStatus': {'type': 'active' if state['running'] else 'idle', 'activeFlags': flags},
                'latestPersistedTurnStatus': 'in_progress' if state['running'] else None,
                'runtimeSource': 'existing DeepSeek Harness Host', 'turns': []}

    @staticmethod
    def projection_turns(values):
        result = []
        for index, turn in enumerate(values.get('turnOutline', [])):
            if not isinstance(turn, dict):
                continue
            started = turn.get('startedAt', turn.get('startTime'))
            completed = turn.get('completedAt', turn.get('endTime'))
            result.append({'turn_id': str(turn.get('turn', index)), 'status': 'completed' if completed else 'in_progress',
                           'started_at': started / 1000 if isinstance(started, (float, int)) else None,
                           'completed_at': completed / 1000 if isinstance(completed, (float, int)) else None})
        return list(reversed(result))

    def message_page(self, thread_id, limit=40, before=None, _empty_pages=0):
        self.workspace_for_thread(thread_id)
        if not 1 <= limit <= 200:
            raise ValueError('Message limit must be between 1 and 200')
        request = {'address': {'kind': 'session', 'sessionId': thread_id}, 'maxMessages': limit}
        if before:
            try:
                cursor = json.loads(base64.urlsafe_b64decode(before + '=' * (-len(before) % 4)))
                if cursor['threadId'] != thread_id or not isinstance(cursor['beforeSeq'], int):
                    raise ValueError()
                request['beforeSeq'] = cursor['beforeSeq']
            except (ValueError, KeyError, TypeError):
                raise ValueError('Invalid DeepSeek history cursor')
        page = self.client.call('session/events', request)
        messages, turns, first_seq, current_turn = [], {}, None, '0'
        for record in page.get('records', []):
            event = record.get('event', record)
            if not isinstance(event, dict) or not isinstance(event.get('seq'), int):
                continue
            first_seq = event['seq'] if first_seq is None else min(first_seq, event['seq'])
            kind, data = event.get('type'), event.get('data', {})
            turn_id = str(data.get('turn', current_turn))
            if kind == 'turn/start':
                current_turn = turn_id
            if kind in {'turn/start', 'turn/end'}:
                turn = turns.setdefault(turn_id, {'turn_id': turn_id, 'status': 'in_progress'})
                turn['started_at' if kind == 'turn/start' else 'completed_at'] = event['time'] / 1000
                if kind == 'turn/end':
                    reason = data.get('reason', {}).get('kind')
                    turn['status'] = 'completed' if reason in {None, 'completed'} and not data.get('error') else 'interrupted' if reason in {'cancelled', 'canceled', 'interrupted'} else 'failed'
            if kind not in {'user/message', 'assistant/message'}:
                continue
            message = data.get('message', data)
            text = content_text(message.get('content', data.get('content', [])))
            if not text:
                continue
            source = message.get('source', data.get('source', {}))
            if kind == 'user/message' and source.get('kind') != 'user':
                continue
            messages.append({'itemId': str(message.get('id') or thread_id + ':' + str(event['seq'])), 'turnId': turn_id,
                             'role': 'user' if kind == 'user/message' else 'assistant', 'phase': 'final', 'text': text,
                             'createdAtMs': event['time'], 'ordinal': event['seq'], 'clientUserMessageId': source.get('rpcId')})
        cursor = None
        if page.get('hasMore') and first_seq is not None:
            cursor = base64.urlsafe_b64encode(json.dumps({'threadId': thread_id, 'beforeSeq': first_seq}, separators=(',', ':')).encode()).decode().rstrip('=')
        # Native pages can contain only generated runtime context. Skip a bounded
        # number of those pages so one upward gesture still reaches human chat.
        if not messages and cursor and _empty_pages < 8:
            older = self.message_page(thread_id, limit, cursor, _empty_pages + 1)
            by_id = {turn['turn_id']: turn for turn in turns.values()}
            for turn in older['turns']:
                by_id[turn['turn_id']] = {**turn, **by_id.get(turn['turn_id'], {})}
            older['turns'] = list(reversed(list(by_id.values())))
            return older
        return {'messages': messages, 'hasMore': bool(page.get('hasMore')), 'olderCursor': cursor,
                'pageSize': limit, 'turns': list(reversed(list(turns.values())))}

    def queue(self, thread_id):
        return [{'id': item['id'], 'input': item.get('content', []), 'placement': item.get('placement'),
                 'clientUserMessageId': item.get('rpcId')} for item in self.state(thread_id).get('queue') or []]

    def delivery(self, thread_id, client_id, previous=None):
        previous = previous or {}
        page = self.message_page(thread_id, 40)
        user = next((m for m in page['messages'] if m.get('clientUserMessageId') == client_id), None)
        user = user or previous.get('receivedUserMessage')
        replies = [m for m in page['messages'] if user and m['role'] == 'assistant' and m['turnId'] == user['turnId'] and m['ordinal'] > user['ordinal']]
        replies = list({m['itemId']: m for m in previous.get('laterAssistantMessages', []) + replies}.values())
        turn = next((turn for turn in page['turns'] if user and turn['turn_id'] == user['turnId']), None)
        return {'accepted': True, 'receivedInHistory': user is not None,
                'receivedUserMessage': user,
                'turnStatus': turn['status'] if turn else previous.get('turnStatus') or ('in_progress' if user else None),
                'laterAssistantMessages': replies[-8:]}

    def receipt(self, row):
        output = json.loads(row['result']) if row['result'] else None
        value = {'operationId': row['id'], 'kind': row['kind'], 'requestStatus': row['status'],
                 'createdAt': row['created_at'], 'result': output}
        if output and output.get('clientUserMessageId'):
            value['delivery'] = self.delivery(output['threadId'], output['clientUserMessageId'], output.get('observedDelivery'))
            if row['kind'] == 'queue_add' and not value['delivery']['receivedInHistory']:
                value['delivery']['accepted'] = False
            if value['delivery']['receivedInHistory'] and value['delivery'] != output.get('observedDelivery'):
                output['observedDelivery'] = value['delivery']
                self.journal.finish(row['id'], row['status'], output)
        return value

    def perform(self, key, kind, payload):
        if not key or len(key) > 200:
            raise ValueError('Idempotency-Key is required')
        with self.lock:
            row, fresh = self.journal.begin(key, kind, payload)
            if not fresh:
                if row['status'] == 'failed':
                    raise DeepSeekError('This request already failed; inspect operation ' + row['id'])
                return dict(self.receipt(row), replayed=True)
            try:
                output = self._apply(kind, payload, row['id'])
                self.journal.finish(row['id'], 'applied', output)
            except (TimeoutError, ConnectionError, BrokenPipeError, OSError):
                self.journal.finish(row['id'], 'outcome_unknown', {'doNotBlindlyRetry': True})
                raise
            except Exception as error:
                self.journal.finish(row['id'], 'failed', {'error': str(error)})
                raise
            return dict(self.receipt(self.journal.get(row['id'])), replayed=False)

    def _apply(self, kind, payload, operation_id):
        tid = payload.get('threadId')
        if kind == 'create':
            workspace = next((w for w in self._workspaces() if w['path'] == payload.get('cwd')), None)
            if workspace is None:
                raise PermissionError('Create DeepSeek chats only in a native workspace')
            created = self.client.call('session/create', {'workspaceId': workspace['id'], 'sessionId': 'session-' + operation_id})
            tid = created['sessionId']
            if payload.get('title', '').strip():
                self.client.call('session/rename', {'sessionId': tid, 'title': payload['title'].strip()[:120]})
            if not payload.get('text', '').strip():
                return {'threadId': tid, 'transport': 'existing DeepSeek Harness Host'}
            kind = 'send'
        self.workspace_for_thread(tid)
        if kind in {'send', 'queue_add'}:
            text = payload.get('text', '').strip()
            if not text or len(text) > 120000:
                raise ValueError('A non-empty instruction up to 120000 characters is required')
            self.client.call('session/prompt', {'sessionId': tid, 'requestId': operation_id,
                'mode': 'queue' if kind == 'queue_add' else 'steer', 'content': [{'type': 'text', 'text': text}], 'clientTimeZone': 'Asia/Shanghai'})
            items = self.queue(tid)
            item = next((item for item in items if item.get('clientUserMessageId') == operation_id), None)
            return {'threadId': tid, 'clientUserMessageId': operation_id, 'deliveryState': 'queued' if item else 'accepted',
                    'item': item, 'transport': 'existing DeepSeek Harness Host'}
        item_id = payload.get('itemId')
        items = self.queue(tid)
        if kind == 'queue_clear':
            ids = set(payload.get('ids', [i['id'] for i in items]))
            removed = []
            for item in items:
                if item['id'] in ids:
                    self.client.call('session/updateQueue', {'sessionId': tid, 'itemId': item['id'], 'action': {'kind': 'remove'}})
                    removed.append(item['id'])
            return {'threadId': tid, 'deletedIds': removed}
        item = next((item for item in items if item['id'] == item_id), None)
        if item is None:
            raise LookupError('DeepSeek queue item is no longer pending')
        actions = {'queue_update': {'kind': 'edit', 'content': [{'type': 'text', 'text': payload.get('text', '').strip()}]},
                   'queue_delete': {'kind': 'remove'}, 'queue_send': {'kind': 'steer'}}
        if kind not in actions:
            raise ValueError('DeepSeek does not expose native queue reordering')
        self.client.call('session/updateQueue', {'sessionId': tid, 'itemId': item_id, 'action': actions[kind]})
        if kind == 'queue_update':
            return {'threadId': tid, 'item': next((i for i in self.queue(tid) if i['id'] == item_id), None)}
        return {'threadId': tid, 'queueId': item_id, 'deleted': kind == 'queue_delete',
                'removedAfterAcceptance': kind == 'queue_send', 'clientUserMessageId': item.get('clientUserMessageId') if kind == 'queue_send' else None}

    def artifacts(self, thread_id):
        workspace = self.workspace_for_thread(thread_id)
        roots = self.artifact_roots + [Path(workspace['path']).resolve()]
        files = {}
        for message in self.message_page(thread_id, 200)['messages']:
            if message['role'] != 'assistant':
                continue
            for match in re.finditer(r'\]\(<?(/[^\n)]+)\)', message['text']):
                path = Path(re.sub(r':\d+$', '', unquote(match.group(1).rstrip('>')))).resolve()
                if not any(path == root or root in path.parents for root in roots) or not path.is_file():
                    continue
                if path.stat().st_size > MAX_ARTIFACT:
                    continue
                ident = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                files[ident] = {'id': ident, 'name': path.name, 'path': str(path), 'size': path.stat().st_size, 'threadId': thread_id}
        return list(files.values())

    def read_artifact(self, thread_id, artifact_id):
        item = next((item for item in self.artifacts(thread_id) if item['id'] == artifact_id), None)
        if item is None:
            raise LookupError('File is not linked from this DeepSeek conversation')
        with Path(item['path']).open('rb') as source:
            content = source.read(MAX_ARTIFACT + 1)
        if len(content) > MAX_ARTIFACT:
            raise ValueError('Artifact exceeds 64 MiB')
        return item, content
