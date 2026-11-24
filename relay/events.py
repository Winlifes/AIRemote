"""Keep a small, account-scoped buffer of invalidations for reconnecting clients."""

from collections import defaultdict, deque
from contextlib import contextmanager
import hashlib
import json
import re
import secrets
import threading
import time


WATCH_TTL = 300
MAX_WATCHES = 8
DEFAULT_CAPACITY = 256
DEVICE_TIMEOUT = 50


class EventFeed:
    def __init__(self, capacity=DEFAULT_CAPACITY, clock=time.monotonic):
        self.condition = threading.Condition()
        self.epoch = secrets.token_hex(12)
        self.clock = clock
        self.entries = defaultdict(lambda: deque(maxlen=capacity))
        self.sequence = defaultdict(int)
        self.subscribers = defaultdict(int)
        self.watches = {}
        self.devices = {}

    @contextmanager
    def subscribe(self, account):
        with self.condition:
            if self.subscribers[account] >= 16:
                raise OverflowError('Too many event streams for this account')
            self.subscribers[account] += 1
        try:
            yield
        finally:
            with self.condition:
                self.subscribers[account] -= 1

    def emit(self, account, kind, data=None):
        with self.condition:
            self.sequence[account] += 1
            cursor = self.sequence[account]
            self.entries[account].append((cursor, kind, data or {}))
            self.condition.notify_all()

    def read(self, account, cursor, timeout=10):
        prefix = hashlib.sha256(f'{self.epoch}:{account}'.encode()).hexdigest()[:16]
        with self.condition:
            match = re.fullmatch(rf'{prefix}:(\d{{1,15}})', cursor or '')
            after = int(match[1]) if match else -1
            ring = self.entries[account]
            current = self.sequence[account]
            if after < (ring[0][0] - 1 if ring else current) or after > current:
                return [(f'{prefix}:{current}', 'reset', {})]
            if after == current:
                self.condition.wait_for(lambda: self.sequence[account] > after, timeout)
                if ring and after < ring[0][0] - 1:
                    return [(f'{prefix}:{self.sequence[account]}', 'reset', {})]
            return [(f'{prefix}:{seq}', kind, data) for seq, kind, data in ring if seq > after]

    def watch(self, account, device, path, provider, public):
        with self.condition:
            current = self.watches.setdefault(device, {})
            previous = current.pop(path, None)
            if len(current) >= MAX_WATCHES:
                del current[next(iter(current))]
            current[path] = (self.clock() + WATCH_TTL, account, provider, public, previous[4] if previous else None)

    def paths(self, device):
        with self.condition:
            current = self.watches.get(device, {})
            now = self.clock()
            for path, watch in tuple(current.items()):
                if watch[0] <= now:
                    current.pop(path)
            if not current:
                self.watches.pop(device, None)
            return list(current)

    def observe(self, device, revisions):
        if not isinstance(revisions, dict):
            return
        with self.condition:
            for path in self.paths(device):
                revision = revisions.get(path)
                if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
                    continue
                expires, account, provider, public, previous = self.watches[device][path]
                if revision != previous:
                    self.watches[device][path] = expires, account, provider, public, revision
                    self.emit(account, 'chat', {'provider': provider, 'threadId': public})

    def changed(self, device, path):
        with self.condition:
            for root in self.paths(device):
                if path.startswith(root + '/'):
                    _, account, provider, public, _ = self.watches[device][root]
                    self.emit(account, 'chat', {'provider': provider, 'threadId': public})

    def heartbeat(self, account, device, healthy):
        with self.condition:
            now = self.clock()
            previous = self.devices.get(device)
            self.devices[device] = (account, healthy, now + DEVICE_TIMEOUT)
            if previous is None or previous[1] != healthy or previous[2] <= now:
                self.emit(account, 'device')

    def disconnect(self, account, device):
        with self.condition:
            self.devices.pop(device, None)
            self.watches.pop(device, None)
            self.emit(account, 'device')

    def expire(self):
        with self.condition:
            now = self.clock()
            for device, (account, _, deadline) in tuple(self.devices.items()):
                if deadline <= now:
                    del self.devices[device]
                    self.emit(account, 'device')
            for device in tuple(self.watches):
                self.paths(device)


def event_frame(cursor, event, data):
    return f'id: {cursor}\nevent: {event}\ndata: {json.dumps(data, separators=(",", ":"))}\n\n'.encode()
