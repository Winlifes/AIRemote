#!/usr/bin/env python3
"""Self-hosted AIRemote relay. Python stdlib; run behind a TLS reverse proxy."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from accounts import Accounts, Conflict, Forbidden, LEGACY_DEVICE, OWNER, SESSION_AGE, digest, password_hash
from protocol import MAX_BODY, MAX_RESULT, validate_request
from routing import Router, Unavailable, combined_state

RESULT_AGE = 15 * 60
VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()


def initialize_config(path, origin, password=None):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    salt = secrets.token_hex(16)
    password = password or secrets.token_urlsafe(18)
    config = {"username": "admin", "password_salt": salt, "password_hash": password_hash(password, salt),
              "owner_token": secrets.token_urlsafe(32), "agent_token": secrets.token_urlsafe(32),
              "public_origin": origin.rstrip("/"), "device_name": "我的 Mac", "secure_cookie": origin.startswith("https://")}
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(config, stream, indent=2)
    return config, password


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.blobs = self.root / "responses"
        self.blobs.mkdir(exist_ok=True, mode=0o700)
        self.path = self.root / "relay.sqlite"
        self.condition = threading.Condition()
        self.last_cleanup = 0
        with self.db() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, csrf TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS attempts(peer TEXT, created REAL);
                CREATE INDEX IF NOT EXISTS attempt_time ON attempts(created);
                CREATE TABLE IF NOT EXISTS device(id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, last_seen REAL,
                    connector_online INTEGER, error TEXT, agent_version TEXT);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, request_key TEXT UNIQUE, fingerprint TEXT,
                    request TEXT, state TEXT, created REAL, expires REAL, lease TEXT, lease_until REAL,
                    attempts INTEGER DEFAULT 0, status INTEGER, headers TEXT, finished REAL);
                CREATE INDEX IF NOT EXISTS pending_jobs ON jobs(state, created);
            """)
            db.execute("UPDATE jobs SET state='result_expired' WHERE state='expired' AND finished IS NOT NULL")
        self.path.chmod(0o600)

    def db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def device(self):
        with self.db() as db:
            row = db.execute("SELECT * FROM device WHERE id=1").fetchone()
        row = dict(row) if row else {}
        row["online"] = bool(row.get("last_seen", 0) > time.time() - 50)
        row["connectorOnline"] = bool(row.get("connector_online")) and row["online"]
        return row

    def submit(self, request):
        fingerprint = digest(json.dumps(request, sort_keys=True, ensure_ascii=False))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM jobs WHERE request_key=?", (request["key"],)).fetchone()
            if old:
                if old["fingerprint"] != fingerprint:
                    raise Conflict("同一请求编号不能用于不同操作。")
                return dict(old)
            if not self.device()["connectorOnline"]:
                raise Unavailable("Mac 连接器离线，请检查 Mac 是否联网并保持运行。")
            if db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','working') AND expires>?", (time.time(),)).fetchone()[0] >= 60:
                raise Unavailable("请求较多，请稍后重试。")
            job_id, created = str(uuid.uuid4()), time.time()
            db.execute("INSERT INTO jobs(id,request_key,fingerprint,request,state,created,expires) VALUES(?,?,?,?,?,?,?)",
                       (job_id, request["key"], fingerprint, json.dumps(request, ensure_ascii=False), "queued", created, created + 300))
            row = dict(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
        with self.condition:
            self.condition.notify_all()
        return row

    def get(self, job_id):
        with self.db() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN attempts>0 THEN 'unknown' ELSE 'expired' END,request='{}' WHERE expires<? AND state IN ('queued','working')", (time.time(),))
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise LookupError("请求不存在或已过期。")
        return dict(row)

    def get_by_key(self, key):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE request_key=?', (key,)).fetchone()
        if row and row['expires'] < time.time() and row['state'] in {'queued', 'working'}:
            return self.get(row['id'])
        return dict(row) if row else None

    def disconnect(self):
        with self.db() as db:
            db.execute('DELETE FROM device')
            db.execute("UPDATE jobs SET state=CASE WHEN attempts>0 THEN 'unknown' ELSE 'expired' END,request='{}' "
                       "WHERE state IN ('queued','working')")
        with self.condition:
            self.condition.notify_all()

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = db.execute("SELECT * FROM jobs WHERE expires>? AND (state='queued' OR (state='working' AND lease_until<? AND attempts<2)) ORDER BY created LIMIT 1", (now, now)).fetchone()
            if not row:
                return None
            lease = secrets.token_urlsafe(24)
            db.execute("UPDATE jobs SET state='working',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?", (lease, now + 150, row["id"]))
            return {"id": row["id"], "lease": lease, "request": json.loads(row["request"])}

    def finish(self, job_id, lease, status, headers, content):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["lease"] != lease or row["state"] not in {"working", "done"}:
                raise Conflict("请求租约已失效。")
            if row["state"] == "done":
                return
            temp = self.blobs / (job_id + ".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(content)
            os.replace(temp, self.blobs / job_id)
            db.execute("UPDATE jobs SET state='done',status=?,headers=?,finished=? WHERE id=?", (status, json.dumps(headers), time.time(), job_id))

    def cleanup(self):
        if self.last_cleanup > time.time() - 60:
            return
        self.last_cleanup = time.time()
        with self.db() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN attempts>0 THEN 'unknown' ELSE 'expired' END,request='{}' WHERE expires<? AND state IN ('queued','working')", (time.time(),))
            rows = db.execute("SELECT id FROM jobs WHERE finished<? AND state='done' LIMIT 100", (time.time() - RESULT_AGE,)).fetchall()
            for row in rows:
                (self.blobs / row["id"]).unlink(missing_ok=True)
                db.execute("UPDATE jobs SET state='result_expired',request='{}',headers=NULL WHERE id=?", (row["id"],))
            db.execute("DELETE FROM jobs WHERE created<? AND state NOT IN ('working','queued')", (time.time() - 86400,))
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM attempts WHERE created<?", (time.time() - 900,))


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, config, state, static):
        self.config = config
        self.store = Store(state)
        self.accounts = Accounts(self.store.root, config)
        self.device_stores = {LEGACY_DEVICE: self.store}
        self.store_lock = threading.Lock()
        for device in self.accounts.devices(include_revoked=True):
            self.device_store(device['id'])
        self.router = Router(self.store.root, self.accounts, self.device_store)
        self.last_account_cleanup = 0
        self.static = Path(static).resolve()
        super().__init__(address, Handler)

    def device_store(self, device):
        with self.store_lock:
            if device not in self.device_stores:
                self.device_stores[device] = Store(self.store.root / 'devices' / device)
            return self.device_stores[device]

    def service_actions(self):
        # Expire cached responses even if the Mac and browsers are both offline.
        now = time.time()
        if self.last_account_cleanup > now - 60:
            return
        self.last_account_cleanup = now
        with self.store_lock:
            stores = list(self.device_stores.values())
        for store in stores:
            store.cleanup()
        self.accounts.cleanup(now)
        self.router.cleanup(now)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIRemote/" + VERSION

    def log_message(self, *_):
        pass

    def respond(self, status, body, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
            content_type = "application/json; charset=utf-8"
        else:
            content_type = "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", (headers or {}).get("Content-Type", content_type))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        for key, value in (headers or {}).items():
            if key.lower() != "content-type":
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def body(self, maximum=MAX_BODY):
        size = int(self.headers.get("Content-Length", "0"))
        if not 0 <= size <= maximum:
            raise ValueError("请求内容过大。")
        value = self.rfile.read(size)
        if len(value) != size:
            raise ValueError("请求内容不完整。")
        return value

    def json_body(self):
        value = json.loads(self.body() or b"{}")
        if not isinstance(value, dict):
            raise ValueError("请求必须为 JSON 对象。")
        return value

    def secret(self, name):
        expected = self.server.config.get(name, "")
        actual = self.headers.get("Authorization", "")
        return len(expected) >= 24 and hmac.compare_digest(actual.encode(), ("Bearer " + expected).encode())

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != self.server.config["public_origin"]:
            raise PermissionError("跨站请求被拒绝。")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("跨站请求被拒绝。")

    def user(self, mutation=False):
        if self.secret("owner_token"):
            return {"csrf": "", "account": OWNER, "username": self.server.config['username'], "api": True}
        self.same_origin()
        try:
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            name = "__Host-airemote_session" if self.server.config.get("secure_cookie", True) else "airemote_session"
            raw = cookie[name].value
        except (KeyError, ValueError):
            raise PermissionError("请先登录。")
        current = self.server.accounts.session(raw)
        if current:
            row = dict(current)
        else:
            # Cookies issued by 0.9.x belong to the original site owner.
            with self.server.store.db() as db:
                legacy = db.execute("SELECT * FROM sessions WHERE token_hash=? AND expires>?", (digest(raw), time.time())).fetchone()
            row = {**legacy, 'account': OWNER, 'username': self.server.config['username'], 'legacy': True} if legacy else None
        if not row:
            raise PermissionError("登录已过期，请重新登录。")
        if mutation and not hmac.compare_digest(self.headers.get("X-AIRemote-CSRF", ""), row["csrf"]):
            raise PermissionError("请刷新页面后重试。")
        return dict(row)

    def cookie(self, token, max_age=SESSION_AGE):
        secure = self.server.config.get("secure_cookie", True)
        name = "__Host-airemote_session" if secure else "airemote_session"
        return name + "=" + token + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=" + str(max_age) + ("; Secure" if secure else "")

    def job_public(self, row):
        return {"id": row["id"], "state": row["state"], "status": row["status"],
                "createdAt": row["created"], "attempts": row["attempts"], "expiresAt": row["expires"]}

    def allow_attempt(self, purpose):
        forwarded = self.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
        peer = digest(purpose + ':' + (forwarded or self.client_address[0]))
        with self.server.store.db() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM attempts WHERE peer=? AND created>?', (peer, time.time() - 900)).fetchone()[0]
            if count >= 20:
                return False
            db.execute('INSERT INTO attempts VALUES(?,?)', (peer, time.time()))
        return True

    def open_session(self, account):
        raw, csrf, username = self.server.accounts.new_session(account)
        return self.respond(200, {'username': username, 'csrf': csrf}, {'Set-Cookie': self.cookie(raw)})

    def agent_route(self, path):
        accounts = self.server.accounts
        authorization = self.headers.get('Authorization', '')
        device = accounts.agent(authorization)
        store = self.server.device_store(device['id'])
        if path == '/api/agent/poll' and self.command == 'POST':
            value = self.json_body()
            with accounts.lock:
                accounts.agent(authorization)
                store.cleanup()
                with store.db() as db:
                    db.execute("INSERT INTO device VALUES(1,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen,connector_online=excluded.connector_online,error=excluded.error,agent_version=excluded.agent_version", (
                        str(value.get('name', device['name']))[:80], time.time(), int(value.get('connectorOnline') is True),
                        str(value.get('error', ''))[:200], str(value.get('version', 'unknown'))[:40]))
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                with accounts.lock:
                    accounts.agent(authorization)
                    job = store.claim()
                if job:
                    return self.respond(200, {'job': job})
                with store.condition:
                    store.condition.wait(timeout=1)
            return self.respond(200, {'job': None})
        parts = path.strip('/').split('/')
        if len(parts) == 5 and parts[:3] == ['api', 'agent', 'jobs'] and parts[4] == 'result' and self.command == 'POST':
            job_id = str(uuid.UUID(parts[3]))
            lease = self.headers.get('X-Relay-Lease', '')
            status = int(self.headers.get('X-Connector-Status', '502'))
            if not 100 <= status <= 599:
                raise ValueError('Invalid status')
            headers = {'Content-Type': self.headers.get('X-Connector-Type', 'application/octet-stream')[:200]}
            for source, dest in [('X-Connector-Disposition', 'Content-Disposition'), ('X-Connector-Etag', 'ETag')]:
                value = self.headers.get(source)
                if value and len(value) < 2000 and '\r' not in value and '\n' not in value:
                    headers[dest] = value
            content = self.body(MAX_RESULT)
            with accounts.lock:
                accounts.agent(authorization)
                store.finish(job_id, lease, status, headers, content)
            return self.respond(200, {'saved': True})
        raise LookupError('Unknown agent endpoint')

    def admin_route(self, path, account):
        if account != OWNER:
            raise Forbidden('需要管理员权限。')
        accounts, router = self.server.accounts, self.server.router
        if path == '/api/admin' and self.command == 'GET':
            return self.respond(200, {'accounts': accounts.profiles(), 'devices': router.devices(None, True),
                                      'invitations': accounts.invitations()})
        if path == '/api/admin/invitations' and self.command == 'POST':
            return self.respond(201, accounts.create_invitation(self.json_body().get('username')))
        parts = path.strip('/').split('/')
        if self.command == 'POST' and len(parts) == 5:
            _, _, resource, identifier, action = parts
            if (resource, action) == ('invitations', 'revoke'):
                accounts.revoke_invitation(identifier)
            elif (resource, action) == ('accounts', 'enabled'):
                enabled = self.json_body().get('enabled')
                with accounts.lock:
                    accounts.set_enabled(identifier, enabled)
                    if not enabled:
                        for device in accounts.devices(identifier):
                            self.server.device_store(device['id']).disconnect()
            elif (resource, action) == ('devices', 'revoke'):
                accounts.revoke_device(identifier)
                self.server.device_store(identifier).disconnect()
            else:
                raise LookupError('找不到此管理操作。')
            return self.respond(200, {'ok': True})
        raise LookupError('找不到此管理页面。')

    def route(self):
        path = urlsplit(self.path).path
        store = self.server.store
        if path == "/api/health" and self.command == "GET":
            return self.respond(200, {"ok": True, "service": "AIRemote relay", "version": VERSION})
        accounts, router = self.server.accounts, self.server.router
        if path in {'/api/login', '/api/register', '/api/pairing/claim'} and self.command == 'POST':
            self.same_origin()
            if not self.allow_attempt(path):
                return self.respond(429, {'error': '尝试次数过多，请稍后重试。'})
            value = self.json_body()
            if path == '/api/login':
                return self.open_session(accounts.authenticate(value.get('username'), value.get('password')))
            if path == '/api/register':
                return self.open_session(accounts.register(value.get('code'), value.get('password')))
            identifier = accounts.claim_pairing(value.get('code'), value.get('tokenHash'), value.get('name'), value.get('platform', 'macos'))
            return self.respond(200, {'deviceId': identifier})
        if path.startswith('/api/agent/'):
            return self.agent_route(path)
        if path.startswith('/api/'):
            session = self.user(self.command != 'GET')
            account = session['account']
            if path.startswith('/api/admin'):
                return self.admin_route(path, account)
            if path == '/api/session' and self.command == 'GET':
                return self.respond(200, {'username': session['username'], 'accountId': account, 'isAdmin': account == OWNER,
                                          'csrf': session['csrf'], 'device': combined_state(router.devices(account))})
            if path == '/api/logout' and self.command == 'POST':
                if session.get('legacy'):
                    with store.db() as db:
                        db.execute('DELETE FROM sessions WHERE token_hash=?', (session['token_hash'],))
                elif session.get('token_hash'):
                    accounts.end_session(session['token_hash'])
                return self.respond(200, {'ok': True}, {'Set-Cookie': self.cookie('', 0)})
            if path == '/api/device' and self.command == 'GET':
                return self.respond(200, combined_state(router.devices(account)))
            if path == '/api/account' and self.command == 'GET':
                return self.respond(200, {'username': session['username'], 'isAdmin': account == OWNER,
                                          'devices': router.devices(account)})
            if path == '/api/account/pairings' and self.command == 'POST':
                return self.respond(201, accounts.create_pairing(account))
            parts = path.strip('/').split('/')
            if (self.command == 'POST' and len(parts) == 5 and parts[:3] == ['api', 'account', 'devices']
                    and parts[4] == 'revoke'):
                accounts.revoke_device(parts[3], account)
                self.server.device_store(parts[3]).disconnect()
                return self.respond(200, {'ok': True})
            if path == '/api/requests' and self.command == 'POST':
                return self.respond(202, self.job_public(router.submit(account, validate_request(self.json_body()))))
            if len(parts) in {3, 4} and parts[:2] == ['api', 'requests'] and self.command == 'GET':
                job_id = str(uuid.UUID(parts[2]))
                if len(parts) == 3:
                    return self.respond(200, self.job_public(router.status(account, job_id)))
                if parts[3] == 'result':
                    status, headers, content = router.result(account, job_id)
                    return self.respond(status, content, headers)
            raise LookupError('Unknown endpoint')
        if self.command != "GET":
            raise LookupError("Unknown endpoint")
        requested = self.server.static / path.lstrip("/")
        resolved = requested.resolve()
        if not resolved.is_relative_to(self.server.static) or "." in Path(path).parts:
            raise PermissionError("Invalid path")
        if not resolved.is_file():
            if Path(path).suffix:
                raise LookupError("File not found")
            resolved = self.server.static / "index.html"
        if not resolved.is_file():
            raise LookupError("Web build is not available")
        return self.respond(200, resolved.read_bytes(), {"Content-Type": mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"})

    def handle_route(self):
        self.connection.settimeout(90)
        try:
            self.route()
        except Forbidden as error:
            self.respond(403, {"error": str(error)})
        except PermissionError as error:
            self.respond(401, {"error": str(error)})
        except LookupError as error:
            self.respond(404, {"error": str(error)})
        except Conflict as error:
            self.respond(409, {"error": str(error)})
        except Unavailable as error:
            self.respond(503, {"error": str(error)})
        except (ValueError, KeyError, TypeError) as error:
            self.respond(400, {"error": str(error)})
        except (OSError, sqlite3.Error):
            self.respond(503, {"error": "服务暂时不可用，请稍后重试。"})

    do_GET = handle_route
    do_POST = handle_route


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--static", required=True)
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    config_path = Path(args.config)
    if config_path.stat().st_mode & 0o077:
        raise PermissionError("Relay config must have mode 0600")
    config = json.loads(config_path.read_text())
    server = RelayServer(("127.0.0.1", args.port), config, args.state, args.static)
    print(json.dumps({"service": "AIRemote relay", "pid": os.getpid(), "listening": "127.0.0.1:" + str(args.port)}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
