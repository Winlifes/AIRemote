"""Read authorized Windows Codex history using SQLite or native rollout files."""

import base64
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit
import uuid

from codex_probe import History, LOCAL_LINK, MAX_DOWNLOAD_BYTES, connect_read_only, find_database
from protocol import UUID, validate_request


MAX_LINE = 2 * 1024 * 1024
READ_ONLY = 'Windows 上可查看 Codex 历史和文件。此电脑的对话采用只读连接。'


def read_records(path):
    with path.open('rb') as source:
        while True:
            offset = source.tell()
            line = source.readline(MAX_LINE)
            if not line:
                return
            if not line.endswith(b'\n'):
                while line and not line.endswith(b'\n'):
                    line = source.readline(MAX_LINE)
                continue
            try:
                value = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(value, dict):
                yield offset, value


def project_id(root):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, 'airemote-windows:' + os.path.normcase(root)))


class CodexWindows:
    def __init__(self, roots, codex_home=None):
        self.home = Path(codex_home or os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()
        self.roots = tuple(Path(root).resolve(strict=True) for root in roots)
        self.sessions = self.home / 'sessions'
        self.rows = {}
        self.expires = 0

    def scope(self, cwd):
        if not isinstance(cwd, str) or not Path(cwd).is_absolute():
            return None
        full = Path(cwd).resolve()
        options = [root for root in self.roots if full.is_relative_to(root)]
        if not options:
            return None
        root = max(options, key=lambda value: len(value.parts))
        return str(root.joinpath(*full.relative_to(root).parts[:1]))

    def catalog(self):
        if time.monotonic() < self.expires:
            return self.rows
        rows = {}
        try:
            with connect_read_only(find_database(self.home, 'state')) as db:
                columns = {row['name'] for row in db.execute('PRAGMA table_info(threads)')}
                fields = ','.join(name for name in ('id', 'cwd', 'name', 'title', 'history_mode', 'rollout_path', 'updated_at', 'archived') if name in columns)
                if {'id', 'cwd'} <= columns:
                    for record in db.execute('SELECT ' + fields + ' FROM threads'):
                        row = dict(record)
                        if re.fullmatch(UUID, row['id']) and not row.get('archived') and (root := self.scope(row['cwd'])):
                            rows[row['id']] = {**row, 'root': root}
        except (OSError, ValueError, sqlite3.Error):
            pass
        if not rows:
            # Fallback for Codex versions that still store their session catalog in rollout files.
            for path in self.sessions.glob('**/rollout-*.jsonl'):
                try:
                    resolved = path.resolve(strict=True)
                    if not resolved.is_relative_to(self.sessions.resolve()):
                        continue
                    with path.open('rb') as source:
                        meta = json.loads(source.readline(65536))
                    data = meta.get('payload', {}) if meta.get('type') == 'session_meta' else {}
                    tid, cwd = data.get('id', ''), data.get('cwd')
                    if re.fullmatch(UUID, tid) and (root := self.scope(cwd)):
                        rows[tid] = {'id': tid, 'cwd': cwd, 'root': root, 'rollout_path': str(path),
                                     'updated_at': int(path.stat().st_mtime), 'title': 'Codex 会话'}
                except (OSError, ValueError, AttributeError, TypeError):
                    continue
        self.rows, self.expires = rows, time.monotonic() + 5
        return rows

    def descriptor(self):
        readable = self.home.is_dir() and (self.sessions.is_dir() or any(self.home.glob('state_*.sqlite')))
        return {'id': 'codex', 'name': 'Codex', 'clientName': 'Codex Windows', 'installed': readable,
                'connected': False, 'readable': readable, 'status': 'history' if readable else 'unavailable',
                'statusLabel': '本地历史可读' if readable else '等待本地会话',
                'features': ['projects', 'messages', 'artifacts'], 'message': READ_ONLY}

    def projects(self):
        paths = sorted({row['root'] for row in self.catalog().values()} or {str(root) for root in self.roots})
        return [{'id': project_id(str(root)), 'name': root.name or str(root), 'path': str(root),
                 'roots': [str(root)], 'writable': False} for root in map(Path, paths)]

    def summary(self, row):
        return {'threadId': row['id'], 'title': row.get('name') or row.get('title') or 'Codex 会话',
                'cwd': row['cwd'], 'projectId': project_id(row['root']), 'updatedAt': row.get('updated_at', 0),
                'runtimeStatus': 'unknown', 'runtimeSource': 'Windows Codex local history',
                'latestPersistedTurnStatus': None, 'turns': [],
                'writeAvailable': False, 'writeMessage': READ_ONLY, 'queueAvailable': False}

    def threads(self, pid):
        roots = [p['path'] for p in self.projects() if p['id'] == pid]
        if not roots:
            raise LookupError('找不到已授权项目。')
        return [self.summary(row) for row in sorted(self.catalog().values(), key=lambda row: row.get('updated_at') or 0, reverse=True)
                if row['root'] in roots]

    def row(self, tid):
        row = self.catalog().get(tid)
        if row is None:
            raise LookupError('找不到此电脑上已授权的 Codex 对话。')
        return row

    def history(self, row):
        if row.get('history_mode') == 'paginated':
            try:
                return History(row['id'], self.roots, self.home)
            except (FileNotFoundError, ValueError):
                pass
        return None

    def rollout(self, row):
        raw_path = row.get('rollout_path')
        candidates = [Path(raw_path)] if isinstance(raw_path, str) else []
        candidates.extend(self.sessions.glob('**/rollout-*-' + row['id'] + '.jsonl'))
        for candidate in candidates:
            try:
                candidate = candidate.resolve(strict=True)
                if candidate.is_relative_to(self.sessions.resolve()) and candidate.is_file():
                    with candidate.open('rb') as source:
                        meta = json.loads(source.readline(65536))
                    payload = meta.get('payload', {})
                    if meta.get('type') == 'session_meta' and payload.get('id') == row['id'] and self.scope(payload.get('cwd')):
                        return candidate
            except (OSError, ValueError, AttributeError):
                continue
        return None

    def rollout_messages(self, row):
        path = self.rollout(row)
        events, items, turns = [], [], {}
        if path is None:
            return events, []
        turn = row['id']
        for offset, record in read_records(path):
            payload = record.get('payload')
            if not isinstance(payload, dict):
                continue
            kind, event = record.get('type'), payload.get('type')
            if kind == 'event_msg' and event in ('task_started', 'turn_started'):
                turn = str(payload.get('turn_id') or offset)
                turns[turn] = {'turn_id': turn, 'status': 'in_progress'}
            elif kind == 'event_msg' and event in ('task_complete', 'turn_completed', 'turn_aborted'):
                finished = str(payload.get('turn_id') or turn)
                turns[finished] = {'turn_id': finished, 'status': 'interrupted' if event == 'turn_aborted' else 'completed'}
            elif kind == 'event_msg' and event in ('user_message', 'agent_message'):
                text = payload.get('message')
                role = 'user' if event == 'user_message' else 'assistant'
                if isinstance(text, str) and text.strip():
                    events.append(self.message(row, offset, turn, role, text, payload.get('phase')))
            elif kind == 'response_item' and event == 'message' and payload.get('role') in ('user', 'assistant'):
                text = '\n'.join(part['text'] for part in payload.get('content', []) if isinstance(part, dict)
                                 and part.get('type') in ('input_text', 'output_text') and isinstance(part.get('text'), str))
                if text.strip() and not text.lstrip().startswith(('<environment_context>', '# AGENTS.md', '<system-reminder>')):
                    items.append(self.message(row, offset, turn, payload['role'], text, payload.get('phase')))
        return events or items, list(reversed(turns.values()))

    @staticmethod
    def message(row, offset, turn, role, text, phase):
        return {'threadId': row['id'], 'turnId': turn, 'itemId': str(offset), 'ordinal': offset,
                'createdAtMs': 0, 'role': role, 'phase': phase, 'text': text[:60000]}

    def page(self, row, limit, before=None):
        history = self.history(row)
        if history:
            try:
                return history.message_page(limit, before)
            except sqlite3.Error:
                pass
        messages, turns = self.rollout_messages(row)
        if before:
            try:
                identifier, offset = json.loads(base64.urlsafe_b64decode(before + '=' * (-len(before) % 4)))
                if identifier != row['id'] or type(offset) is not int or offset < 0:
                    raise ValueError()
            except (ValueError, TypeError):
                raise ValueError('历史记录位置已变化，请刷新。')
            messages = [item for item in messages if item['ordinal'] < offset]
        page = messages[-limit:]
        older = len(messages) > limit
        cursor = base64.urlsafe_b64encode(json.dumps([row['id'], page[0]['ordinal']]).encode()).decode().rstrip('=') if older else None
        return {'messages': page, 'turns': turns, 'olderCursor': cursor, 'hasMore': older, 'pageSize': limit}

    def detail(self, row):
        summary = self.summary(row)
        history = self.history(row)
        try:
            turns = history.snapshot()['turns'] if history else self.rollout_messages(row)[1]
        except (ValueError, sqlite3.Error):
            turns = []
        return {**summary, 'turns': turns[:20], 'latestPersistedTurnStatus': turns[0]['status'] if turns else None}

    def safe_file(self, raw):
        if raw.startswith('file:'):
            uri = urlsplit(raw)
            if uri.netloc:
                raise PermissionError('仅支持本地文件。')
            raw = unquote(uri.path)
            if os.name == 'nt' and re.match(r'^/[A-Za-z]:', raw):
                raw = raw[1:]
        raw = re.sub(r':\d+(?::\d+)?$', '', raw)
        if not Path(raw).is_absolute() or raw.startswith(('\\\\', '//')):
            raise PermissionError('仅支持本地文件。')
        path = Path(raw).resolve(strict=True)
        if any(part.casefold() in {'.git', '.ssh', '.local', '.codex'} or part.casefold().startswith('.env') for part in path.parts):
            raise PermissionError('此文件受保护。')
        if not path.is_file() or not any(path.is_relative_to(root) for root in self.roots):
            raise PermissionError('文件超出授权目录。')
        return path

    def artifacts(self, row):
        results = {}
        for item in self.page(row, 200)['messages']:
            if item['role'] != 'assistant':
                continue
            for match in LOCAL_LINK.finditer(item['text']):
                try:
                    path = self.safe_file(unquote((match.group(1) or match.group(2)).strip()))
                    size = path.stat().st_size
                    if size > MAX_DOWNLOAD_BYTES:
                        continue
                except (OSError, ValueError):
                    continue
                aid = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                results[aid] = {'id': aid, 'name': path.name, 'path': str(path), 'size': size, 'threadId': row['id'], 'itemId': item['itemId']}
        return results


class Connector(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, token, backend):
        self.token, self.backend = token, backend
        super().__init__(('127.0.0.1', port), Handler)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, status, value, headers=None):
        data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Content-Type', (headers or {}).get('Content-Type', 'application/json; charset=utf-8'))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if headers and headers.get('Content-Disposition'):
            self.send_header('Content-Disposition', headers['Content-Disposition'])
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        expected = ('Bearer ' + self.server.token).encode()
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode(), expected):
            return self.reply(401, {'error': '未授权。'})
        try:
            if self.path == '/api/health':
                return self.reply(200, {'ok': True, 'platform': 'windows'})
            validate_request({'method': 'GET', 'path': self.path, 'key': 'windows-observer'})
            url = urlsplit(self.path)
            path, backend = url.path, self.server.backend
            if path == '/api/providers':
                return self.reply(200, {'providers': [backend.descriptor()]})
            if path == '/api/projects':
                return self.reply(200, {'projects': backend.projects(), 'messagePageSize': 40})
            match = re.fullmatch('/api/projects/(' + UUID + ')/threads', path)
            if match:
                return self.reply(200, {'threads': backend.threads(match[1])})
            match = re.fullmatch('/api/threads/(' + UUID + r')(?:/(messages|queue|artifacts)(?:/([0-9a-f]{24})/download)?)?', path)
            if not match:
                raise LookupError('此 Windows 连接器暂不支持这个操作。')
            row = backend.row(match[1])
            res, aid = match.group(2, 3)
            if res == 'messages':
                params = parse_qs(url.query)
                return self.reply(200, backend.page(row, int(params.get('limit', [40])[0]), params.get('before', [None])[0]))
            if res == 'queue':
                return self.reply(200, {'items': []})
            if res == 'artifacts':
                artifacts = backend.artifacts(row)
                if not aid:
                    return self.reply(200, {'artifacts': list(artifacts.values())})
                if aid not in artifacts:
                    raise LookupError('找不到此对话的关联文件。')
                file = backend.safe_file(artifacts[aid]['path'])
                with file.open('rb') as source:
                    data = source.read(MAX_DOWNLOAD_BYTES + 1)
                if len(data) > MAX_DOWNLOAD_BYTES:
                    raise ValueError('文件超过 64 MiB。')
                return self.reply(200, data, {'Content-Type': mimetypes.guess_type(file.name)[0] or 'application/octet-stream',
                    'Content-Disposition': "attachment; filename*=UTF-8''" + quote(file.name, safe='')})
            return self.reply(200, backend.detail(row))
        except LookupError as error:
            return self.reply(404, {'error': str(error)})
        except ValueError as error:
            return self.reply(400, {'error': str(error)})
        except (OSError, sqlite3.Error):
            return self.reply(503, {'error': '暂时无法读取 Windows Codex 数据。'})

    def do_POST(self):
        self.reply(403, {'error': READ_ONLY})

    do_PATCH = do_PUT = do_DELETE = do_POST
