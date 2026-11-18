"""Read local Claude Code sessions and run authorized CLI turns on the Mac."""

import base64
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import unquote, urlsplit
import uuid

from codex_probe import LOCAL_LINK, MAX_DOWNLOAD_BYTES


UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
MAX_RECORD = 2 * 1024 * 1024
MAX_TEXT = 60000
PROJECT_NAMESPACE = uuid.UUID('6e1af47d-fc93-4b86-b1b1-2608a127dede')
HELP = 'https://code.claude.com/docs/en/desktop'


class ClaudeError(RuntimeError):
    pass


def object_json(value):
    try:
        value = json.loads(value)
    except (ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) else {}


def stamp(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
        except (ValueError, OverflowError):
            pass
    return 0


def records(source, budget=None):
    """Yield complete, bounded JSONL records with append-stable byte offsets."""
    while budget is None or source.tell() < budget:
        offset = source.tell()
        limit = MAX_RECORD if budget is None else min(MAX_RECORD, budget - offset)
        line = source.readline(limit)
        if not line:
            break
        if not line.endswith(b'\n'):
            while line and not line.endswith(b'\n') and (budget is None or source.tell() < budget):
                size = MAX_RECORD if budget is None else min(MAX_RECORD, budget - source.tell())
                line = source.readline(size)
            continue
        value = object_json(line)
        if value:
            yield offset, value


def visible_text(record, role):
    if any(record.get(flag) for flag in ('isSidechain', 'isMeta', 'isCompactSummary', 'isVisibleInTranscriptOnly', 'isApiErrorMessage')):
        return ''
    message = record.get('message')
    if not isinstance(message, dict) or message.get('role') != role:
        return ''
    if str(message.get('model', '')).startswith('<'):
        return ''
    origin = record.get('origin')
    if role == 'user' and (record.get('sourceToolAssistantUUID')
        or isinstance(origin, dict) and origin.get('kind') not in {None, 'human'}):
        return ''
    content = message.get('content')
    if isinstance(content, list):
        content = '\n'.join(block['text'] for block in content if isinstance(block, dict)
            and block.get('type') == 'text' and isinstance(block.get('text'), str))
    if not isinstance(content, str) or content.lstrip().startswith((
        '<local-command-', '<command-name>', '<command-message>', '<system-reminder>',
        '<bash-input>', '<bash-stdout>', '<bash-stderr>', '<task-notification>',
    )):
        return ''
    return content[:MAX_TEXT]


def project_id(cwd):
    return str(uuid.uuid5(PROJECT_NAMESPACE, str(cwd)))


@dataclass
class Session:
    id: str
    cwd: Path
    log: Path
    title: str
    updated: int


class ClaudeCLI:
    def __init__(self, home):
        self.home = home
        self.lock = threading.RLock()
        self.cache = {}

    def cached(self, key, ttl, read):
        with self.lock:
            expires, value = self.cache.get(key, (0, None))
            if time.monotonic() >= expires:
                value = read()
                self.cache[key] = (time.monotonic() + ttl, value)
            return value

    def binary(self):
        def locate():
            for path in [
                self.home / '.local/bin/claude',
                Path('/opt/homebrew/bin/claude'),
                Path('/usr/local/bin/claude'),
            ]:
                if path.is_file() and os.access(path, os.X_OK):
                    return path
            bundle = self.home / 'Library/Application Support/Claude/claude-code'
            candidates = [p for p in bundle.glob('*/*/claude.app/Contents/MacOS/claude')
                          if p.is_file() and os.access(p, os.X_OK)]
            def version(path):
                return tuple(int(number) for number in re.findall(r'\d+', path.relative_to(bundle).parts[0]))
            return max(candidates, key=version, default=None)
        return self.cached('binary', 20, locate)

    def environment(self):
        # AIRemote watches the standard local Code store. CLI calls must use
        # the same store, including when the launcher inherits a custom profile.
        return {**os.environ, 'CLAUDE_CONFIG_DIR': str(self.home / '.claude')}

    def command(self, arguments, cwd=None):
        binary = self.binary()
        if not binary:
            return None
        try:
            result = subprocess.run([str(binary), *arguments], cwd=cwd or self.home, env=self.environment(),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout if result.returncode == 0 else None

    def live(self, fresh=False):
        def query():
            try:
                rows = json.loads(self.command(['agents', '--json']) or b'null')
            except (ValueError, UnicodeError):
                return None
            if not isinstance(rows, list):
                return None
            return {row['sessionId']: row for row in rows if isinstance(row, dict)
                    and isinstance(row.get('sessionId'), str) and re.fullmatch(UUID, row['sessionId'])}
        return query() if fresh else self.cached('live', 1.5, query)

    def write_mode(self):
        def check():
            status = object_json(self.command(['auth', 'status', '--json']) or b'{}')
            if status.get('loggedIn') is not True or status.get('authMethod') != 'api_key':
                return None
            help_text = self.command(['--help']) or b''
            if b'--permission-prompts' not in help_text:
                return None
            return 'manual' if b'"manual"' in help_text else 'default'
        return self.cached('write', 15, check)

    def start(self, tid, cwd, title, mode, resume, output):
        binary = self.binary()
        if binary is None:
            raise ClaudeError('Mac 上找不到 Claude Code CLI。')
        args = ['--print', '--verbose', '--input-format', 'stream-json', '--output-format', 'stream-json',
                '--permission-mode', mode, '--permission-prompts', 'none']
        args += ['--resume', tid] if resume else ['--session-id', tid, '--name', title]
        return subprocess.Popen([str(binary), *args], cwd=cwd, env=self.environment(),
                                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                                start_new_session=True)


class ClaudeAdapter:
    def __init__(self, journal, artifact_roots, workspace_roots, discover_desktop=False, home=None, cli=None):
        self.journal = journal
        self.home = Path(home or Path.home())
        self.projects_path = self.home / '.claude/projects'
        self.desktop = self.home / 'Library/Application Support/Claude/claude-code-sessions'
        self.artifact_roots = tuple(artifact_roots)
        self.workspace_roots = tuple(workspace_roots)
        self.discover_desktop = discover_desktop
        self.cli = cli or ClaudeCLI(self.home)
        self.lock = threading.RLock()
        self.running = {}
        self.closed = False
        self.catalog = {}
        self.catalog_until = 0
        self.heads = {}
        self.views = OrderedDict()

    def writable_cwd(self, cwd):
        return cwd.is_dir() and any(cwd.is_relative_to(root) for root in self.workspace_roots)

    def descriptor(self):
        try:
            info = plistlib.loads(Path('/Applications/Claude.app/Contents/Info.plist').read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            info = {}
        binary = self.cli.binary()
        installed = bool(info or binary)
        readable = installed or self.projects_path.is_dir()
        live = self.cli.live() if readable else None
        write = live is not None and bool(self.cli.write_mode())
        connected = live is not None and (bool(live) or write)
        return {
            'id': 'claude', 'name': 'Claude', 'clientName': 'Claude Code',
            'version': info.get('CFBundleShortVersionString'), 'installed': installed,
            'connected': connected, 'readable': readable,
            'status': 'connected' if connected else 'history' if readable else 'not_installed',
            'statusLabel': ('已连接 · 可续聊' if write else '已连接 · 只读') if connected
                else '本地历史可读' if readable else '未安装',
            'features': ['projects', 'messages', 'runtime-status', 'artifacts'] + (['send', 'create'] if write else []),
            'message': ('可从手机新建本地会话，或继续已经关闭的会话。' if write
                        else '支持 Mac 上的 Claude Code 本地会话。发送需要在 Mac 配置 Claude Code 的 API 凭据。'),
            'helpUrl': HELP,
        }

    def desktop_records(self):
        result = {}
        for path in self.desktop.glob('*/*/local_*.json'):
            try:
                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD or not path.resolve().is_relative_to(self.desktop.resolve()):
                    continue
                row = object_json(path.read_bytes())
            except OSError:
                continue
            tid = row.get('cliSessionId')
            if isinstance(tid, str) and re.fullmatch(UUID, tid):
                result[tid] = row
        return result

    def first_info(self, path, tid, info):
        # Project discovery reads a small prefix of each log. Full messages are
        # decoded only after opening the corresponding session.
        key = (info.st_dev, info.st_ino, info.st_birthtime if hasattr(info, 'st_birthtime') else 0)
        cached = self.heads.get(path)
        if cached and cached[0] == key and cached[1] <= info.st_size:
            return cached[2]
        cwd, first = None, ''
        try:
            with path.open('rb') as source:
                for _, record in records(source, budget=256 * 1024):
                    if record.get('sessionId') != tid or record.get('isSidechain'):
                        continue
                    if cwd is None and isinstance(record.get('cwd'), str) and Path(record['cwd']).is_absolute():
                        cwd = Path(record['cwd'])
                    if record.get('type') == 'user' and not first:
                        first = visible_text(record, 'user').strip().split('\n', 1)[0][:120]
                    if cwd and first:
                        break
        except OSError:
            return None, ''
        if cwd and (first or info.st_size >= 256 * 1024):
            self.heads[path] = key, info.st_size, (cwd, first)
        return cwd, first

    def sessions(self, refresh=False):
        with self.lock:
            if not refresh and time.monotonic() < self.catalog_until:
                return dict(self.catalog)
            desktop = self.desktop_records()
            catalog = {}
            for path in self.projects_path.glob('*/*.jsonl'):
                tid = path.stem
                if not re.fullmatch(UUID, tid):
                    continue
                try:
                    if path.is_symlink() or not path.resolve().is_relative_to(self.projects_path.resolve()):
                        continue
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    meta = desktop.get(tid, {})
                    if meta.get('isArchived') is True:
                        continue
                    cwd, first = self.first_info(path, tid, info)
                    reported = meta.get('cwd')
                    if isinstance(reported, str) and Path(reported).is_absolute():
                        cwd = Path(reported)
                    if not cwd:
                        continue
                    cwd = cwd.resolve()
                    if not (self.writable_cwd(cwd) or self.discover_desktop and tid in desktop):
                        continue
                except (OSError, ValueError, RuntimeError):
                    continue
                title = meta.get('title')
                title = title.strip()[:160] if isinstance(title, str) and title.strip() else first or 'Claude Code 对话'
                updated = max(stamp(meta.get('lastActivityAt')), int(info.st_mtime * 1000))
                if tid not in catalog or catalog[tid].updated < updated:
                    catalog[tid] = Session(tid, cwd, path, title, updated)
            self.heads = {path: value for path, value in self.heads.items() if path.exists()}
            self.catalog, self.catalog_until = catalog, time.monotonic() + 4
            return dict(catalog)

    def projects(self):
        paths = {row.cwd for row in self.sessions().values()} | set(self.workspace_roots)
        return [{'id': project_id(path), 'name': path.name or str(path), 'path': str(path),
                 'roots': [str(path)], 'writable': path in self.workspace_roots and path.is_dir()}
                for path in sorted(paths, key=lambda value: (value.name.casefold(), str(value)))]

    def threads(self, pid):
        projects = self.projects()
        if not any(project['id'] == pid for project in projects):
            raise LookupError('找不到这个 Claude Code 项目。')
        with self.lock:
            rows = {**self.sessions(), **{tid: row for tid, (row, _) in self.running.items() if tid not in self.catalog}}
        live = self.cli.live()
        return [self.summary(row, live) for row in sorted(rows.values(), key=lambda item: item.updated, reverse=True)
                if project_id(row.cwd) == pid]

    def row(self, tid):
        with self.lock:
            row = self.sessions().get(tid)
            if row is None and tid in self.running:
                row = self.running[tid][0]
            if row is None:
                row = self.sessions(refresh=True).get(tid)
        if row is None:
            raise LookupError('找不到已授权的本地 Claude Code 会话。')
        return row

    def summary(self, row, live):
        with self.lock:
            running = row.id in self.running
        native = (live or {}).get(row.id)
        status = (native or {}).get('status')
        flags = []
        if running or status in ('working', 'running', 'starting', 'busy', 'in_progress'):
            runtime = 'active'
        elif status in ('waiting', 'waiting_for_input', 'needs_input', 'waiting_for_permission', 'blocked'):
            runtime, flags = 'active', ['needsInput']
        elif status in ('idle', 'done', 'completed', 'stopped'):
            runtime = 'idle'
        else:
            runtime = 'notLoaded' if live is not None and native is None else None
        reason = ''
        if running:
            reason = 'Claude Code 正在处理本轮指令。'
        elif not self.writable_cwd(row.cwd):
            reason = '此 Claude Code 项目目前只允许查看。'
        elif not self.cli.write_mode():
            reason = 'Mac 上的 Claude Code CLI 尚未配置可用的 API 凭据。'
        elif live is None:
            reason = '暂时无法确认 Claude Code 会话是否空闲。'
        elif native is not None:
            reason = '请在 Mac 关闭这条会话，然后从手机续聊。'
        return {'threadId': row.id, 'title': row.title, 'cwd': str(row.cwd), 'projectId': project_id(row.cwd),
                'runtimeStatus': {'type': runtime, 'activeFlags': flags} if runtime else None,
                'runtimeSource': 'AIRemote Claude Code process' if running else 'claude agents --json' if live is not None else None,
                'latestPersistedTurnStatus': None, 'turns': [],
                'writeAvailable': not reason, 'writeMessage': reason}

    def session(self, tid):
        return self.summary(self.row(tid), self.cli.live())

    def view(self, row):
        try:
            info = row.log.stat()
        except FileNotFoundError:
            return [], []
        signature = (row.log, info.st_ino, info.st_size, info.st_mtime_ns)
        with self.lock:
            cached = self.views.get(row.id)
            if cached and cached[0] == signature:
                self.views.move_to_end(row.id)
                return cached[1:]
        messages, turns, indices, turn = [], [], {}, None
        with row.log.open('rb') as source:
            for ordinal, entry in records(source, budget=info.st_size):
                role = entry.get('type')
                if role not in ('user', 'assistant') or entry.get('sessionId') != row.id:
                    continue
                item = entry.get('uuid')
                if not isinstance(item, str) or not re.fullmatch(UUID, item):
                    continue
                text = visible_text(entry, role)
                if not text.strip():
                    continue
                ts = stamp(entry.get('timestamp'))
                if item in indices:
                    index, own_turn = indices[item]
                else:
                    index, own_turn = len(messages), turn
                    if role == 'user':
                        turn = {'turn_id': item, 'started_at': ts / 1000 if ts else None, 'status': 'in_progress'}
                        turns.append(turn)
                        own_turn = turn
                    messages.append({})
                    indices[item] = index, own_turn
                stop = entry['message'].get('stop_reason')
                if role == 'assistant' and own_turn and stop in ('end_turn', 'stop_sequence'):
                    own_turn.update(status='completed', completed_at=ts / 1000 if ts else None)
                prior = messages[index]
                messages[index] = {
                    'itemId': item, 'threadId': row.id, 'turnId': own_turn['turn_id'] if own_turn else row.id,
                    'role': role, 'phase': 'commentary' if role == 'assistant' and stop == 'tool_use' else 'final_answer',
                    'text': text, 'createdAtMs': ts, 'ordinal': prior.get('ordinal', ordinal + 1),
                }
        turns.reverse()
        with self.lock:
            self.views[row.id] = (signature, messages, turns)
            self.views.move_to_end(row.id)
            while len(self.views) > 4:
                self.views.popitem(last=False)
        return messages, turns

    def message_page(self, tid, limit=40, before=None):
        if not 1 <= limit <= 200:
            raise ValueError('Claude Code 每页支持 1 到 200 条消息。')
        messages, turns = self.view(self.row(tid))
        end = len(messages)
        if before:
            try:
                payload = base64.urlsafe_b64decode(before + '=' * (-len(before) % 4)).decode()
                account, anchor = payload.split(':', 1)
                if account != tid or not re.fullmatch(UUID, anchor):
                    raise ValueError()
                end = next(i for i, message in enumerate(messages) if message['itemId'] == anchor)
            except (ValueError, UnicodeError, StopIteration):
                raise ValueError('Claude Code 历史已变化，请刷新对话再查看更早消息。')
        start = max(0, end - limit)
        page = messages[start:end]
        cursor = base64.urlsafe_b64encode(f"{tid}:{page[0]['itemId']}".encode()).decode().rstrip('=') if start else None
        ids = {message['turnId'] for message in page}
        return {'messages': page, 'turns': [turn for turn in turns if turn['turn_id'] in ids],
                'pageSize': limit, 'hasMore': start > 0, 'olderCursor': cursor}

    def artifacts(self, tid):
        row = self.row(tid)
        messages, _ = self.view(row)
        artifacts = {}
        for message in messages:
            if message['role'] != 'assistant':
                continue
            for match in LOCAL_LINK.finditer(message['text']):
                raw = unquote((match.group(1) or match.group(2)).strip())
                if raw.startswith('file:'):
                    parsed = urlsplit(raw)
                    raw = parsed.path if not parsed.netloc else ''
                if not raw.startswith('/') or raw.startswith('//'):
                    continue
                try:
                    path = Path(re.sub(r':\d+(?::\d+)?$', '', raw)).resolve(strict=True)
                    if not self.allowed_artifact(path):
                        continue
                    info = path.stat()
                    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_DOWNLOAD_BYTES:
                        continue
                except (OSError, ValueError, RuntimeError):
                    continue
                aid = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                artifacts[aid] = {'id': aid, 'name': path.name, 'path': str(path), 'size': info.st_size,
                                  'threadId': tid, 'itemId': message['itemId'], 'turnId': message['turnId']}
        return list(artifacts.values())

    def allowed_artifact(self, path):
        if any(part in ('.local', '.claude', '.git', '.ssh') or part.startswith('.env')
               for part in path.parts):
            return False
        return any(path.is_relative_to(root) for root in self.artifact_roots)

    def read_artifact(self, tid, aid):
        artifact = next((item for item in self.artifacts(tid) if item['id'] == aid), None)
        if artifact is None:
            raise LookupError('这个文件没有关联到当前 Claude Code 会话。')
        path = Path(artifact['path']).resolve(strict=True)
        if not self.allowed_artifact(path):
            raise PermissionError('文件不在授权的下载目录中。')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise PermissionError('当前文件已不可下载。')
            content = source.read(MAX_DOWNLOAD_BYTES + 1)
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise ValueError('文件超过 64 MiB。')
        return artifact, content

    def receipt(self, item):
        output = object_json(item['result'] or '{}')
        status = item['status']
        delivery = {'accepted': status in ('in_flight', 'applied'), 'receivedInHistory': False,
                    'turnStatus': 'in_progress' if status == 'in_flight' else None, 'laterAssistantMessages': []}
        tid, user_id = output.get('threadId'), output.get('clientUserMessageId')
        if tid and user_id:
            try:
                messages, _ = self.view(self.row(tid))
            except (LookupError, OSError):
                messages = []
            user = next((message for message in messages if message['itemId'] == user_id and message['role'] == 'user'), None)
            if user:
                delivery['receivedInHistory'] = True
                delivery['receivedUserMessage'] = user
                delivery['laterAssistantMessages'] = [message for message in messages if message['role'] == 'assistant'
                                                     and message['turnId'] == user_id][-8:]
            if status == 'applied' and user:
                delivery['turnStatus'] = 'completed'
            elif status == 'failed':
                delivery['turnStatus'] = 'failed'
        return {'operationId': item['id'], 'kind': item['kind'], 'requestStatus': status,
                'createdAt': item['created_at'], 'result': output, 'delivery': delivery}

    def perform(self, key, kind, payload):
        if not key or len(key) > 180:
            raise ValueError('缺少稳定的请求编号。')
        if set(payload) - ({'text', 'threadId'} if kind == 'send' else {'cwd', 'title', 'text'}):
            raise ValueError('不支持的 Claude Code 请求字段。')
        text = payload.get('text')
        if not isinstance(text, str) or not text.strip() or len(text) > 120000:
            raise ValueError('请输入首条指令，长度最多为 120000 字。')
        with self.lock:
            if self.closed:
                raise ClaudeError('Mac 上的 AIRemote 连接器正在停止。')
            item, fresh = self.journal.begin(key, kind, payload)
            if not fresh:
                return self.receipt(item)
            tid = payload.get('threadId') if kind == 'send' else item['id']
            output = {'threadId': tid, 'clientUserMessageId': str(uuid.uuid4()),
                      'executionState': 'starting', 'transport': 'Claude Code CLI'}
            stream = None
            try:
                if kind == 'send':
                    row = self.row(tid)
                else:
                    cwd, title = payload.get('cwd'), payload.get('title')
                    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(title, str) or not title.strip():
                        raise ValueError('请选择工作目录并填写会话名称。')
                    cwd = Path(cwd).resolve(strict=True)
                    if cwd not in self.workspace_roots:
                        raise PermissionError('请在 Mac 配置这个项目的创建权限。')
                    row = Session(tid, cwd, self.projects_path / '.pending' / (tid + '.jsonl'),
                                  title.strip()[:120], int(time.time() * 1000))
                if not self.writable_cwd(row.cwd):
                    raise PermissionError('此 Claude Code 项目目前只允许查看。')
                mode = self.cli.write_mode()
                if not mode:
                    raise ClaudeError('请先在 Mac 配置 Claude Code CLI 的 API 凭据。')
                live = self.cli.live(fresh=True)
                if live is None:
                    raise ClaudeError('暂时无法确认 Claude Code 会话是否空闲。')
                if tid in self.running or tid in live:
                    raise ClaudeError('会话仍由 Claude Code 打开或正在运行，请关闭会话后再续聊。')
                if len(self.running) >= 2:
                    raise ClaudeError('Mac 上已有两条 Claude Code 指令在执行，请稍后发送。')
                stream = tempfile.TemporaryFile()
                self.journal.finish(item['id'], 'in_flight', output)
                process = self.cli.start(tid, row.cwd, row.title, mode, kind == 'send', stream)
                self.running[tid] = (row, process)
                worker = threading.Thread(target=self.run, args=(item['id'], row, process, stream, text, output), daemon=True)
                worker.start()
            except (LookupError, PermissionError, ValueError, OSError, ClaudeError) as error:
                if stream:
                    stream.close()
                message = str(error) if isinstance(error, (ValueError, ClaudeError, PermissionError, LookupError)) else '无法启动本地 Claude Code。'
                self.journal.finish(item['id'], 'failed', {**output, 'executionState': 'failed', 'error': message})
                raise
            return self.receipt(self.journal.get(item['id']))

    def run(self, operation, row, process, stream, text, output):
        status, error = 'outcome_unknown', 'Claude Code 执行结果待核对。'
        try:
            user = {'type': 'user', 'session_id': row.id, 'uuid': output['clientUserMessageId'],
                    'message': {'role': 'user', 'content': [{'type': 'text', 'text': text}]},
                    'parent_tool_use_id': None}
            process.communicate((json.dumps(user, ensure_ascii=False) + '\n').encode(), timeout=30 * 60)
            stream.seek(0)
            result = None
            for _, entry in records(stream):
                if entry.get('type') == 'result':
                    result = entry
            if result and result.get('session_id') == row.id:
                if result.get('is_error') or result.get('subtype') != 'success' or process.returncode:
                    status, error = 'failed', 'Claude Code 未完成本轮指令，请在 Mac 核对 CLI 凭据或权限。'
                else:
                    matching = self.sessions(refresh=True).get(row.id)
                    messages, _ = self.view(matching) if matching else ([], [])
                    user_id = output['clientUserMessageId']
                    received = any(message['role'] == 'user' and message['itemId'] == user_id
                                   for message in messages)
                    replied = any(message['role'] == 'assistant' and message['turnId'] == user_id
                                  and message['phase'] == 'final_answer' for message in messages)
                    if received and replied:
                        status, error = 'applied', ''
            elif process.returncode and process.returncode > 0:
                status, error = 'failed', 'Claude Code 未启动本轮，请在 Mac 核对 CLI 登录和会话。'
        except subprocess.TimeoutExpired:
            self.interrupt(process)
        except (OSError, ValueError, LookupError):
            pass
        finally:
            stream.close()
            final = {**output, 'executionState': 'completed' if status == 'applied' else status}
            if error:
                final['error'] = error
            with self.lock:
                self.journal.finish(operation, status, final)
                self.running.pop(row.id, None)
                self.catalog_until = 0

    @staticmethod
    def interrupt(process):
        try:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
        except ProcessLookupError:
            pass
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def close(self):
        with self.lock:
            self.closed = True
            processes = [process for _, process in self.running.values()]
        for process in processes:
            self.interrupt(process)
