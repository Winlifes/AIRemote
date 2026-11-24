#!/usr/bin/env python3
"""Mac -> relay outbound agent. Local token never leaves the Mac."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from local_lock import exclusive_lock
from protocol import MAX_RESULT, chat_watch_path, validate_request

VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()


class NoRelayRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def acquire_agent_lock(config_path):
    lock_path = Path(str(Path(config_path).resolve()) + '.lock')
    try:
        return exclusive_lock(lock_path)
    except OSError as error:
        raise RuntimeError('Another AIRemote agent already uses this configuration') from error


class Agent:
    def __init__(self, config, update_lock=None):
        self.config = config
        self.base = config["relay_url"].rstrip("/")
        parsed = urlsplit(self.base)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}):
            raise ValueError("Relay requires HTTPS (except loopback testing)")
        local = urlsplit(config.get("connector_url", "http://127.0.0.1:8765"))
        if local.scheme != "http" or local.hostname != "127.0.0.1" or local.path not in {"", "/"} or local.username or local.password:
            raise ValueError("Connector must use a loopback HTTP URL")
        self.connector = local.geturl().rstrip("/")
        token_path = Path(config["connector_token_file"])
        if os.name != 'nt' and token_path.stat().st_mode & 0o077:
            raise PermissionError("Connector token must have mode 0600")
        self.local_token = token_path.read_text().strip()
        self.stop = threading.Event()
        self.relay_http = build_opener(NoRelayRedirect)
        self.connector_http = build_opener(ProxyHandler({}), NoRelayRedirect)
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.jobs = {}
        self.update_lock = update_lock
        self.watch = []

    def remote(self, path, data=None, headers=None, timeout=35):
        combined = {"Authorization": "Bearer " + self.config["agent_token"], **(headers or {})}
        request = Request(self.base + path, data=data, headers=combined, method="POST")
        with self.relay_http.open(request, timeout=timeout) as response:
            return json.load(response)

    def local(self, method, path, body=None, key=None, timeout=90):
        headers = {"Authorization": "Bearer " + self.local_token}
        data = None
        if method != "GET":
            data = json.dumps(body or {}, ensure_ascii=False).encode()
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        request = Request(self.connector + path, data=data, headers=headers, method=method)
        try:
            response = self.connector_http.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_RESULT + 1)
            if len(content) > MAX_RESULT:
                raise ValueError("Connector result exceeds 64 MiB")
            return response.code, dict(response.headers), content

    def revisions(self):
        result = {}
        for path in self.watch:
            if self.stop.is_set():
                break
            try:
                status, _, content = self.local('GET', path + '/messages?limit=1', timeout=1.5)
                if status == 200:
                    result[path] = hashlib.sha256(content).hexdigest()
            except (OSError, TimeoutError, ValueError):
                pass
        return result

    def execute(self, job):
        try:
            request = validate_request(job["request"])
        except ValueError:
            status, headers = 400, {'Content-Type': 'application/json'}
            content = json.dumps({'error': '连接器不支持这类请求，请检查网页与连接器版本。'}).encode()
        else:
            try:
                status, headers, content = self.local(request["method"], request["path"], request["body"], request["key"])
            except Exception as error:
                # A write may have executed before a timeout. A failed read
                # never implies an AI instruction was executed.
                message = 'Mac 读取暂时失败，请刷新重试。' if request['method'] == 'GET' else 'Mac 执行结果暂不确定，请先回读对话；重试会复用原请求编号。'
                status, headers = 502, {"Content-Type": "application/json"}
                content = json.dumps({"error": message, "detail": type(error).__name__}).encode()
        outgoing = {"X-Relay-Lease": job["lease"], "X-Connector-Status": str(status),
                    "X-Connector-Type": headers.get("Content-Type", "application/octet-stream"),
                    "Content-Type": "application/octet-stream"}
        for source, target in [("Content-Disposition", "X-Connector-Disposition"), ("ETag", "X-Connector-Etag")]:
            if source in headers:
                outgoing[target] = headers[source]
        for attempt in range(4):
            try:
                self.remote("/api/agent/jobs/" + job["id"] + "/result", content, outgoing, timeout=60)
                return
            except HTTPError as error:
                if error.code in {401, 403, 409}:
                    raise
            except (URLError, TimeoutError, OSError):
                pass
            if self.stop.wait(min(2 ** attempt, 8)):
                return
        raise ConnectionError("Could not upload result; lease will be reconciled")

    def run(self):
        print(json.dumps({"agent": VERSION, "relay": self.base, "pid": os.getpid(), "mode": "outbound-only"}), flush=True)
        delay = 1
        guard = None
        try:
            while not self.stop.is_set():
                try:
                    for key, future in list(self.jobs.items()):
                        if future.done():
                            try:
                                future.result()
                            except Exception as error:
                                print(json.dumps({"requestId": key, "error": type(error).__name__}), flush=True)
                            del self.jobs[key]
                    if guard is not None and not self.jobs:
                        guard.close(); guard = None
                        if self.stop.wait(.06):
                            break
                    if len(self.jobs) >= 6:
                        self.stop.wait(.2)
                        continue
                    if self.update_lock and guard is None:
                        import fcntl
                        guard = os.fdopen(os.open(self.update_lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600), 'r+b')
                        try:
                            fcntl.flock(guard.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                        except BlockingIOError:
                            guard.close(); guard = None
                            self.stop.wait(.25)
                            continue
                        except BaseException:
                            guard.close(); guard = None
                            raise
                    online = False
                    try:
                        status, _, _ = self.local("GET", "/api/health", timeout=2)
                        online = status == 200
                    except (OSError, URLError, TimeoutError):
                        pass
                    payload = {"name": self.config.get("device_name", "我的 Mac"), "version": VERSION,
                               "connectorOnline": online, "error": "" if online else "本地连接器不可用",
                               'watchSupported': True, 'revisions': self.revisions() if online else {}}
                    data = self.remote("/api/agent/poll", json.dumps(payload).encode(), {"Content-Type": "application/json"})
                    delay = 1
                    watch = data.get('watch')
                    self.watch = [path for path in watch[:8] if chat_watch_path(path)] if isinstance(watch, list) else []
                    job = data.get("job")
                    if job and job["id"] not in self.jobs:
                        self.jobs[job["id"]] = self.executor.submit(self.execute, job)
                except (OSError, URLError, TimeoutError, ValueError) as error:
                    if guard is not None and not self.jobs:
                        guard.close(); guard = None
                    if isinstance(error, HTTPError) and error.code in {401, 403}:
                        print(json.dumps({"relayConnection": "registration_revoked"}), flush=True)
                        self.stop.set()
                        continue
                    print(json.dumps({"relayConnection": "retrying", "error": type(error).__name__, "retrySeconds": delay}), flush=True)
                    self.stop.wait(delay)
                    delay = min(delay * 2, 30)
        finally:
            self.executor.shutdown(wait=True)
            if guard is not None:
                guard.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument('--update-lock', type=Path)
    args = parser.parse_args()
    path = Path(args.config)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise PermissionError("Agent config must have mode 0600")
    with acquire_agent_lock(path):
        agent = Agent(json.loads(path.read_text()), args.update_lock)
        signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
        signal.signal(signal.SIGINT, lambda *_: agent.stop.set())
        agent.run()


if __name__ == "__main__":
    main()
