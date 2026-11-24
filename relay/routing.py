"""Merge account-owned device catalogs and route each action to its source device."""

from contextlib import closing, contextmanager
import json
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import time
from urllib.parse import urlsplit
import uuid

from accounts import Conflict, LEGACY_DEVICE, OWNER, digest
from protocol import UUID, validate_request


CATALOG_WAIT = 12
PROJECT_NAMESPACE = uuid.UUID('26b830b9-9188-4c0e-bbd0-3bb30c527303')
CATALOGS = {'/providers', '/projects', '/threads', '/capabilities'}


class Unavailable(RuntimeError):
    pass


def endpoint(path):
    parsed = urlsplit(path)
    match = re.match(r'/api/providers/(deepseek|claude)(/.*)', parsed.path)
    return (match[1], '/api/providers/' + match[1], match[2]) if match else ('codex', '/api', parsed.path[4:])


def combined_state(devices):
    return {
        'online': any(row['online'] for row in devices),
        'connectorOnline': any(row['connectorOnline'] for row in devices),
        'deviceCount': len(devices),
        'onlineCount': sum(row['connectorOnline'] for row in devices),
        'catalogRevision': digest(','.join(sorted(row['id'] for row in devices if row['connectorOnline'])))[:16],
        'name': devices[0]['name'] if len(devices) == 1 else '我的设备',
    }


class Router:
    def __init__(self, root, accounts, stores):
        self.path = Path(root) / 'routes.sqlite'
        self.accounts = accounts
        self.stores = stores
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS routes(
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, request_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, path TEXT NOT NULL, aggregate INTEGER NOT NULL,
                    targets TEXT NOT NULL, created REAL NOT NULL, settled TEXT,
                    UNIQUE(account,request_key));
                CREATE TABLE IF NOT EXISTS aliases(
                    account TEXT NOT NULL, provider TEXT NOT NULL, kind TEXT NOT NULL,
                    device TEXT NOT NULL, native TEXT NOT NULL, public TEXT NOT NULL,
                    PRIMARY KEY(account,provider,kind,device,native));
                CREATE INDEX IF NOT EXISTS aliases_public ON aliases(account,provider,kind,public);
            ''')
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def record_alias(self, db, account, provider, kind, device, native, public):
        db.execute('''INSERT INTO aliases VALUES(?,?,?,?,?,?) ON CONFLICT(account,provider,kind,device,native)
                     DO UPDATE SET public=excluded.public WHERE public!=excluded.public''',
                   (account, provider, kind, device, native, public))

    def bindings(self, account, provider, kind, public):
        with self.db() as db:
            return db.execute('SELECT device,native FROM aliases WHERE account=? AND provider=? AND kind=? AND public=? ORDER BY device,native',
                              (account, provider, kind, public)).fetchall()

    def watch_targets(self, account, provider, public):
        devices = {row['id'] for row in self.accounts.devices(account)}
        bindings = self.bindings(account, provider, 'threads', public)
        if not bindings and LEGACY_DEVICE in devices:
            return [(LEGACY_DEVICE, public)]
        return [(row['device'], row['native']) for row in bindings if row['device'] in devices]

    def devices(self, account, include_revoked=False):
        results = []
        for device in self.accounts.devices(account, include_revoked):
            current = self.stores(device['id']).device() if not device['revoked'] else {}
            name = current.get('name', device['name']) if device['id'] == LEGACY_DEVICE else device['name']
            results.append({'id': device['id'], 'account': device['account'], 'name': name, 'platform': device['platform'],
                            'createdAt': device['created'], 'revoked': device['revoked'] is not None,
                            'online': current.get('online', False), 'connectorOnline': current.get('connectorOnline', False),
                            'lastSeen': current.get('last_seen')})
        return results

    def select_targets(self, account, request, devices):
        path, method = request['path'], request['method']
        provider, prefix, local = endpoint(path)
        online = [row['id'] for row in devices if row['connectorOnline']]
        if not online:
            raise Unavailable('没有在线的电脑，请在账号设置中配对设备或启动桌面连接器。')
        if method == 'GET' and local in CATALOGS:
            return True, {device: path for device in online}
        if local == '/threads' and method == 'POST':
            cwd = request['body'].get('cwd')
            if not isinstance(cwd, str):
                raise ValueError('请选择工作目录。')
            known = {row['device'] for row in self.bindings(account, provider, 'workspace', cwd)}
            target = next((device for device in online if device in known), None)
            if target is None and not known and len(online) == 1:
                target = online[0]
            if target is None:
                raise Unavailable('此工作目录所在的电脑未就绪，请刷新项目后重试。')
            return False, {target: path}
        match = re.fullmatch(r'/(projects|threads|operations)/((?:session-)?' + UUID + r')(/.*)?', local)
        if not match:
            raise LookupError('找不到此操作。')
        kind, public, suffix = match.groups()
        suffix = suffix or ''
        bindings = self.bindings(account, provider, kind, public)
        allowed = {row['id'] for row in devices}
        targets = {row['device']: row['native'] for row in bindings if row['device'] in allowed}
        if not bindings and LEGACY_DEVICE in allowed:
            targets = {LEGACY_DEVICE: public}
        if not targets:
            raise LookupError('找不到账号下的此项目、对话或操作。')
        query = urlsplit(path).query
        routed = {device: f'{prefix}/{kind}/{native}{suffix}' + ('?' + query if query else '')
                  for device, native in targets.items() if device in online}
        if not routed:
            raise Unavailable('这个对话所在的电脑暂时离线。')
        return kind == 'projects', routed

    def submit(self, account, request):
        fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
        with self.accounts.lock, self.db() as db:
            if not self.accounts.enabled(account):
                raise PermissionError('账号已停用。')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM routes WHERE account=? AND request_key=?', (account, request['key'])).fetchone()
            devices = self.devices(account)
            if row:
                if row['fingerprint'] != fingerprint:
                    raise Conflict('同一请求编号不能用于不同操作。')
                targets, aggregate, rid = json.loads(row['targets']), row['aggregate'], row['id']
            else:
                aggregate, targets = self.select_targets(account, request, devices)
                rid = str(uuid.uuid4())
                db.execute('INSERT INTO routes VALUES(?,?,?,?,?,?,?,?,NULL)',
                           (rid, account, request['key'], fingerprint, request['path'], aggregate, json.dumps(targets), time.time()))
        queued = False
        for device, path in targets.items():
            # A device revocation also wakes the waiting worker and invalidates its pending jobs.
            with self.accounts.lock:
                if not self.accounts.enabled(account):
                    raise PermissionError('账号已停用。')
                if device not in {item['id'] for item in self.accounts.devices(account)}:
                    continue
                try:
                    self.stores(device).submit(validate_request({**request, 'path': path}))
                    queued = True
                except Unavailable:
                    if not aggregate:
                        raise
        if not queued:
            raise Unavailable('没有可处理此请求的电脑。')
        return self.status(account, rid)

    def route(self, account, rid):
        with self.db() as db:
            row = db.execute('SELECT * FROM routes WHERE account=? AND id=?', (account, rid)).fetchone()
        if row:
            return dict(row)
        if account == OWNER and any(device['id'] == LEGACY_DEVICE for device in self.accounts.devices(account)):
            job = self.stores(LEGACY_DEVICE).get(rid)
            return {'id': rid, 'legacy': job}
        raise LookupError('请求不存在或已过期。')

    def children(self, account, row):
        active = {item['id'] for item in self.accounts.devices(account)}
        return {device: self.stores(device).get_by_key(row['request_key'])
                for device in json.loads(row['targets']) if device in active}

    def settle(self, row, children):
        if row['settled'] is not None:
            return json.loads(row['settled'])
        complete = all(child and child['state'] not in {'queued', 'working'} for child in children.values())
        if not complete and time.time() - row['created'] < CATALOG_WAIT:
            return None
        good = [device for device, child in children.items() if child and child['state'] == 'done' and 200 <= child['status'] < 300]
        with self.db() as db:
            db.execute('UPDATE routes SET settled=? WHERE id=? AND settled IS NULL', (json.dumps(good), row['id']))
            return json.loads(db.execute('SELECT settled FROM routes WHERE id=?', (row['id'],)).fetchone()[0])

    def status(self, account, rid):
        row = self.route(account, rid)
        if 'legacy' in row:
            return row['legacy']
        children = self.children(account, row)
        if not children:
            raise LookupError('请求不存在或设备已解绑。')
        if not row['aggregate']:
            child = next(iter(children.values()))
            if child:
                return {**child, 'id': rid}
        else:
            ready = self.settle(row, children)
            if ready is not None:
                active = [children[device] for device in ready if device in children and children[device]]
                if ready and not any(job['state'] == 'done' for job in active):
                    return {**row, 'state': 'result_expired', 'attempts': 0, 'status': None, 'expires': row['created'] + 300}
                return {**row, 'state': 'done', 'attempts': 0, 'status': 200 if active else 503, 'expires': row['created'] + 300}
        return {**row, 'state': 'queued', 'attempts': 0, 'status': None, 'expires': row['created'] + 300}

    def load_result(self, device, job):
        if not job or job['state'] != 'done':
            raise Conflict('请求尚未完成或响应已过期。')
        blob = self.stores(device).blobs / job['id']
        try:
            return job['status'], json.loads(job['headers']), blob.read_bytes()
        except FileNotFoundError as error:
            raise LookupError('响应已过期，请重新读取。') from error

    def result(self, account, rid):
        row = self.route(account, rid)
        if 'legacy' in row:
            return self.load_result(LEGACY_DEVICE, row['legacy'])
        children = self.children(account, row)
        provider, _, local = endpoint(row['path'])
        if not children:
            raise LookupError('请求不存在或设备已解绑。')
        if not row['aggregate']:
            device, child = next(iter(children.items()))
            status, headers, content = self.load_result(device, child)
            if status < 300 and not local.endswith('/download'):
                with self.db() as db:
                    content = json.dumps(self.rewrite(db, account, provider, device, json.loads(content)), ensure_ascii=False).encode()
            return status, headers, content
        ready = self.settle(row, children)
        if ready is None:
            raise Conflict('正在汇总在线 Mac 的结果。')
        if not ready:
            return 503, {}, {'error': '暂时无法读取任何在线 Mac 的数据，请稍后重试。'}
        values = []
        for device in ready:
            job = children.get(device)
            if job and job['state'] == 'done':
                _, _, content = self.load_result(device, job)
                values.append((device, json.loads(content)))
        if not values:
            raise LookupError('响应已过期，请重新读取。')
        return 200, {}, self.combine(account, provider, local, values)

    def mapped_id(self, db, account, provider, kind, device, native):
        if not isinstance(native, str) or not re.fullmatch(r'(?:session-)?' + UUID, native):
            return native
        if kind == 'projects':
            row = db.execute('SELECT public FROM aliases WHERE account=? AND provider=? AND kind=? AND device=? AND native=?',
                             (account, provider, kind, device, native)).fetchone()
            return row['public'] if row else native
        if device == LEGACY_DEVICE:
            return native
        public = str(uuid.uuid5(uuid.UUID(device), f'{provider}/{kind}/{native}'))
        if native.startswith('session-'):
            public = 'session-' + public
        self.record_alias(db, account, provider, kind, device, native, public)
        return public

    def rewrite(self, db, account, provider, device, value):
        if isinstance(value, list):
            return [self.rewrite(db, account, provider, device, item) for item in value]
        if not isinstance(value, dict):
            return value
        kinds = {'threadId': 'threads', 'thread_id': 'threads', 'conversationId': 'threads',
                 'operationId': 'operations', 'projectId': 'projects'}
        return {key: self.mapped_id(db, account, provider, kinds[key], device, item) if key in kinds
                else self.rewrite(db, account, provider, device, item) for key, item in value.items()}

    def combine(self, account, provider, local, values):
        if local == '/providers':
            providers = {}
            for _, value in values:
                for item in value.get('providers', []):
                    previous = providers.get(item['id'])
                    if previous is None:
                        providers[item['id']] = item
                        continue
                    preferred = max([previous, item], key=lambda row: (bool(row.get('connected')), bool(row.get('readable')), bool(row.get('installed'))))
                    providers[item['id']] = {**preferred, 'features': sorted(set(previous.get('features', [])) | set(item.get('features', [])))}
            return {'providers': list(providers.values())}
        if local == '/capabilities':
            first = values[0][1]
            workspaces = list(dict.fromkeys(path for _, value in values for path in value.get('workspaces', [])))
            return {**first, 'workspaces': workspaces}
        with self.db() as db:
            if local == '/projects':
                projects, roots = {}, {}
                for device, value in values:
                    names = set()
                    for project in value.get('projects', []):
                        native, name = project.get('id'), project.get('name', '')
                        if not isinstance(native, str) or not re.fullmatch(UUID, native) or not isinstance(name, str):
                            continue
                        key = name.strip().casefold() or project.get('path', '')
                        if key in names:
                            key = f'{key}/{project.get("path", native)}'
                        names.add(key)
                        public = str(uuid.uuid5(PROJECT_NAMESPACE, f'{account}/{provider}/{key}'))
                        self.record_alias(db, account, provider, 'projects', device, native, public)
                        current = projects.setdefault(public, {**project, 'id': public, 'roots': [], 'writable': False})
                        writable = project.get('writable', True)
                        current['writable'] = current['writable'] or writable
                        directory_list = roots.setdefault(public, [])
                        for path in project.get('roots', []):
                            if not isinstance(path, str) or not (path.startswith('/') or PureWindowsPath(path).is_absolute()):
                                continue
                            if path not in directory_list:
                                directory_list.append(path)
                            if writable:
                                self.record_alias(db, account, provider, 'workspace', device, path, path)
                                if path not in current['roots']:
                                    current['roots'].append(path)
                for public, item in projects.items():
                    if not item['writable']:
                        item['roots'] = roots[public]
                return {'projects': sorted(projects.values(), key=lambda item: (item['name'].casefold(), item['id'])), 'messagePageSize': 40}
            threads = [self.rewrite(db, account, provider, device, item)
                       for device, value in values for item in value.get('threads', [])]
            threads.sort(key=lambda item: item.get('updatedAt') or 0, reverse=True)
            return {'threads': threads}

    def cleanup(self, now):
        with self.db() as db:
            db.execute('DELETE FROM routes WHERE created<?', (now - 86400,))
