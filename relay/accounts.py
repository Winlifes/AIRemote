"""Account authentication, invitations, and account-owned device registrations."""

from contextlib import closing, contextmanager
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid


OWNER = 'owner'
LEGACY_DEVICE = 'legacy'
INVITE_AGE = 86400
PAIR_AGE = 600
SESSION_AGE = 7 * 86400


class Conflict(RuntimeError):
    pass


class Forbidden(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()


def token_value(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', value):
        raise ValueError('请输入完整的验证码。')
    return value


def device_name(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 80:
        raise ValueError('请填写 1–80 个字符的设备名称。')
    return value.strip()


class Accounts:
    def __init__(self, root, config):
        self.path = Path(root) / 'accounts.sqlite'
        self.config = config
        self.lock = threading.RLock()
        with self.db() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    salt TEXT NOT NULL, password TEXT NOT NULL, enabled INTEGER NOT NULL,
                    created REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY, account TEXT NOT NULL, name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE, created REAL NOT NULL, revoked REAL,
                    platform TEXT NOT NULL DEFAULT 'macos');
                CREATE INDEX IF NOT EXISTS devices_account ON devices(account);
                CREATE TABLE IF NOT EXISTS sessions(
                    token_hash TEXT PRIMARY KEY, account TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS invitations(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    code_hash TEXT NOT NULL UNIQUE, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS pairings(
                    code_hash TEXT PRIMARY KEY, account TEXT NOT NULL, expires REAL NOT NULL, device TEXT);
                CREATE INDEX IF NOT EXISTS pairings_account ON pairings(account);
            ''')
            db.execute('''INSERT INTO accounts VALUES(?,?,?,?,1,?) ON CONFLICT(id) DO UPDATE SET
                       username=excluded.username,salt=excluded.salt,password=excluded.password''',
                       (OWNER, config['username'], config['password_salt'], config['password_hash'], time.time()))
            if 'platform' not in {row['name'] for row in db.execute('PRAGMA table_info(devices)')}:
                db.execute("ALTER TABLE devices ADD COLUMN platform TEXT NOT NULL DEFAULT 'macos'")
            db.execute('''INSERT INTO devices(id,account,name,token_hash,created,revoked) VALUES(?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET
                       token_hash=excluded.token_hash''', (LEGACY_DEVICE, OWNER, config.get('device_name', '我的 Mac'),
                                                          digest(config.get('agent_token', '')), time.time()))
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def authenticate(self, username, password):
        if (not isinstance(username, str) or not isinstance(password, str) or
                len(username) > 128 or len(password) > 1024):
            raise PermissionError('账号或密码不正确。')
        with self.db() as db:
            row = db.execute('SELECT * FROM accounts WHERE username=?', (username.strip(),)).fetchone()
        # Unknown usernames cost the same password calculation as known ones.
        salt = row['salt'] if row else self.config['password_salt']
        expected = row['password'] if row else self.config['password_hash']
        correct = hmac.compare_digest(password_hash(password, salt), expected)
        if not row or not row['enabled'] or not correct:
            raise PermissionError('账号或密码不正确。')
        return row['id']

    def session(self, raw):
        with self.db() as db:
            return db.execute('''SELECT sessions.*,accounts.username FROM sessions JOIN accounts
                              ON sessions.account=accounts.id WHERE token_hash=? AND expires>? AND enabled=1''',
                              (digest(raw), time.time())).fetchone()

    def new_session(self, account):
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT username FROM accounts WHERE id=? AND enabled=1', (account,)).fetchone()
            if not row:
                raise PermissionError('账号已停用。')
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', (digest(raw), account, csrf, time.time() + SESSION_AGE))
        return raw, csrf, row['username']

    def enabled(self, account):
        with self.db() as db:
            return bool(db.execute('SELECT 1 FROM accounts WHERE id=? AND enabled=1', (account,)).fetchone())

    def end_session(self, token_hash):
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (token_hash,))

    def create_invitation(self, username):
        if not isinstance(username, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{2,31}', username.strip()):
            raise ValueError('账号名使用 3–32 个字母、数字、点、下划线或连字符。')
        username = username.strip()
        code, invite_id = secrets.token_urlsafe(24), str(uuid.uuid4())
        expires = time.time() + INVITE_AGE
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM accounts WHERE username=?', (username,)).fetchone():
                raise Conflict('这个账号名已使用。')
            db.execute('''INSERT INTO invitations VALUES(?,?,?,?) ON CONFLICT(username) DO UPDATE SET
                       id=excluded.id,code_hash=excluded.code_hash,expires=excluded.expires''',
                       (invite_id, username, digest(code), expires))
        return {'id': invite_id, 'username': username, 'code': code, 'expiresAt': expires}

    def register(self, code, password):
        code_hash = digest(token_value(code))
        if not isinstance(password, str) or not 12 <= len(password) <= 1024:
            raise ValueError('密码至少 12 个字符，最多 1024 个字符。')
        with self.db() as db:
            if not db.execute('SELECT 1 FROM invitations WHERE code_hash=? AND expires>?', (code_hash, time.time())).fetchone():
                raise ValueError('邀请码无效或已过期。')
        salt = secrets.token_hex(16)
        hashed = password_hash(password, salt)
        account = str(uuid.uuid4())
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            invitation = db.execute('SELECT username FROM invitations WHERE code_hash=? AND expires>?', (code_hash, time.time())).fetchone()
            if not invitation:
                raise ValueError('邀请码无效或已使用。')
            db.execute('INSERT INTO accounts VALUES(?,?,?,?,1,?)', (account, invitation['username'], salt, hashed, time.time()))
            db.execute('DELETE FROM invitations WHERE code_hash=?', (code_hash,))
        return account

    def invitations(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,username,expires AS expiresAt FROM invitations WHERE expires>? ORDER BY expires', (time.time(),))]

    def revoke_invitation(self, invite_id):
        with self.db() as db:
            db.execute('DELETE FROM invitations WHERE id=?', (invite_id,))

    def create_pairing(self, account):
        code = secrets.token_urlsafe(24)
        expires = time.time() + PAIR_AGE
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM accounts WHERE id=? AND enabled=1', (account,)).fetchone():
                raise PermissionError('账号已停用。')
            db.execute('DELETE FROM pairings WHERE account=? AND device IS NULL', (account,))
            db.execute('INSERT INTO pairings VALUES(?,?,?,NULL)', (digest(code), account, expires))
        return {'code': code, 'expiresAt': expires}

    def claim_pairing(self, code, token_hash, name, platform='macos'):
        code_hash = digest(token_value(code))
        name = device_name(name)
        if platform not in ('macos', 'windows'):
            raise ValueError('此设备平台暂不支持。')
        if not isinstance(token_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', token_hash):
            raise ValueError('设备凭据格式无效。')
        with self.lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT pairings.* FROM pairings JOIN accounts ON pairings.account=accounts.id
                              WHERE code_hash=? AND expires>? AND enabled=1''', (code_hash, time.time())).fetchone()
            if not row:
                raise ValueError('配对码无效或已过期。')
            if row['device']:
                same = db.execute('SELECT id FROM devices WHERE id=? AND token_hash=? AND revoked IS NULL',
                                  (row['device'], token_hash)).fetchone()
                if not same:
                    raise Conflict('这个配对码已使用。')
                return row['device']
            if db.execute('SELECT 1 FROM devices WHERE token_hash=?', (token_hash,)).fetchone():
                raise Conflict('请使用新的设备凭据。')
            device = str(uuid.uuid4())
            db.execute('INSERT INTO devices VALUES(?,?,?,?,?,NULL,?)', (device, row['account'], name, token_hash, time.time(), platform))
            db.execute('UPDATE pairings SET device=? WHERE code_hash=?', (device, code_hash))
            return device

    def devices(self, account=None, include_revoked=False):
        query = 'SELECT id,account,name,created,revoked,platform FROM devices WHERE 1=1'
        args = ()
        if account is not None:
            query += ' AND account=?'
            args = (account,)
        if not include_revoked:
            query += ' AND revoked IS NULL'
        with self.db() as db:
            return [dict(row) for row in db.execute(query + ' ORDER BY created,id', args)]

    def agent(self, authorization):
        if not authorization.startswith('Bearer ') or not 31 <= len(authorization) <= 128:
            raise PermissionError('设备连接凭据已失效，请重新配对电脑。')
        with self.db() as db:
            device = db.execute('''SELECT devices.id,devices.account,devices.name FROM devices JOIN accounts
                                ON devices.account=accounts.id WHERE token_hash=? AND revoked IS NULL AND enabled=1''',
                                (digest(authorization[7:]),)).fetchone()
        if not device:
            raise PermissionError('设备连接凭据已失效，请重新配对电脑。')
        return dict(device)

    def revoke_device(self, device, account=None):
        with self.lock, self.db() as db:
            row = db.execute('SELECT account FROM devices WHERE id=?', (device,)).fetchone()
            if not row or account is not None and row['account'] != account:
                raise LookupError('找不到设备。')
            db.execute('UPDATE devices SET revoked=? WHERE id=? AND revoked IS NULL', (time.time(), device))
            return row['account']

    def profiles(self):
        with self.db() as db:
            return [dict(row) for row in db.execute('SELECT id,username,enabled,created FROM accounts ORDER BY created')]

    def set_enabled(self, account, enabled):
        if account == OWNER:
            raise Conflict('管理员账号始终启用。')
        if not isinstance(enabled, bool):
            raise ValueError('请选择启用或停用。')
        with self.lock, self.db() as db:
            if not db.execute('UPDATE accounts SET enabled=? WHERE id=?', (enabled, account)).rowcount:
                raise LookupError('找不到账号。')
            if not enabled:
                db.execute('DELETE FROM sessions WHERE account=?', (account,))
                db.execute('DELETE FROM pairings WHERE account=? AND device IS NULL', (account,))

    def cleanup(self, now):
        with self.db() as db:
            for table in ('sessions', 'invitations', 'pairings'):
                db.execute(f'DELETE FROM {table} WHERE expires<?', (now,))
