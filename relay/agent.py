#!/usr/bin/env python3
"""Mac -> relay outbound agent. Local token never leaves the Mac."""
import argparse
import fcntl
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from protocol import MAX_RESULT, validate_request

VERSION = (Path(__file__).resolve().parents[1] / 'VERSION').read_text().strip()


def acquire_agent_lock(config_path):
    lock_path = Path(str(Path(config_path).resolve()) + '.lock')
    lock = open(lock_path, 'a+')
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        lock.close()
        raise RuntimeError('Another AIRemote agent already uses this configuration') from error
    return lock


class Agent:
    def __init__(self, config):
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
        if token_path.stat().st_mode & 0o077:
            raise PermissionError("Connector token must have mode 0600")
        self.local_token = token_path.read_text().strip()
        self.stop = threading.Event()
        self.executor = ThreadPoolExecutor(max_workers=6)
        self.jobs = {}

    def remote(self, path, data=None, headers=None, timeout=35):
        combined = {"Authorization": "Bearer " + self.config["agent_token"], **(headers or {})}
        request = Request(self.base + path, data=data, headers=combined, method="POST")
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def local(self, method, path, body=None, key=None):
        headers = {"Authorization": "Bearer " + self.local_token}
        data = None
        if method != "GET":
            data = json.dumps(body or {}, ensure_ascii=False).encode()
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
        request = Request(self.connector + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=90)
        except HTTPError as error:
            response = error
        with response:
            content = response.read(MAX_RESULT + 1)
            if len(content) > MAX_RESULT:
                raise ValueError("Connector result exceeds 64 MiB")
            return response.code, dict(response.headers), content

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
        while not self.stop.is_set():
            try:
                for key, future in list(self.jobs.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception as error:
                            print(json.dumps({"requestId": key, "error": type(error).__name__}), flush=True)
                        del self.jobs[key]
                if len(self.jobs) >= 6:
                    self.stop.wait(.2)
                    continue
                online = False
                try:
                    status, _, _ = self.local("GET", "/api/health")
                    online = status == 200
                except (OSError, URLError, TimeoutError):
                    pass
                payload = {"name": self.config.get("device_name", "我的 Mac"), "version": VERSION,
                           "connectorOnline": online, "error": "" if online else "本地 Codex 连接器不可用"}
                data = self.remote("/api/agent/poll", json.dumps(payload).encode(), {"Content-Type": "application/json"})
                delay = 1
                job = data.get("job")
                if job and job["id"] not in self.jobs:
                    self.jobs[job["id"]] = self.executor.submit(self.execute, job)
            except (OSError, URLError, TimeoutError, ValueError) as error:
                print(json.dumps({"relayConnection": "retrying", "error": type(error).__name__, "retrySeconds": delay}), flush=True)
                self.stop.wait(delay)
                delay = min(delay * 2, 30)
        self.executor.shutdown(wait=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    path = Path(args.config)
    if path.stat().st_mode & 0o077:
        raise PermissionError("Agent config must have mode 0600")
    with acquire_agent_lock(path):
        agent = Agent(json.loads(path.read_text()))
        signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
        signal.signal(signal.SIGINT, lambda *_: agent.stop.set())
        agent.run()


if __name__ == "__main__":
    main()
