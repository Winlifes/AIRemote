"""Read the unprojected part of a Codex paginated rollout."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from contextlib import contextmanager
import gzip
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
import uuid


MAX_RECORD = 16 * 1024 * 1024
MAX_ANCESTORS = 32
UUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
ROLLOUT_NAME = re.compile(rf'rollout-.*?-({UUID})(?:_({UUID}))?\.jsonl(?:\.(?:zst|gz))?$')
MESSAGE_ROLES = {'UserMessage': 'user', 'userMessage': 'user',
                 'AgentMessage': 'assistant', 'agentMessage': 'assistant'}
TURN_STATUSES = {'task_started': 'inProgress', 'turn_started': 'inProgress',
                 'task_complete': 'completed', 'turn_completed': 'completed',
                 'turn_aborted': 'interrupted'}


def is_subagent(source=None, thread_source=None):
    if thread_source == 'subagent' or source == 'subagent':
        return True
    if isinstance(source, str) and source.lstrip().startswith('{'):
        try:
            source = json.loads(source)
        except ValueError:
            return False
    return isinstance(source, dict) and 'subagent' in source


def spawned_threads(db):
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_spawn_edges'").fetchone():
        return {row[0] for row in db.execute('SELECT child_thread_id FROM thread_spawn_edges')}
    return set()


def timestamp_ms(value):
    try:
        return int(datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() * 1000)
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


@dataclass(frozen=True)
class RolloutTail:
    cutoff: int
    messages: tuple
    turns: dict


@dataclass(frozen=True)
class HistoryBase:
    rollout_id: str
    ordinal: int
    offset: int


def history_base(metadata):
    base = metadata.get('history_base')
    if base is None:
        return None
    if not isinstance(base, dict):
        raise ValueError('Invalid Codex history reference')
    ordinal, offset = base.get('end_ordinal_exclusive'), base.get('end_byte_offset')
    if type(ordinal) is not int or ordinal < 1 or type(offset) is not int or offset < 1:
        raise ValueError('Invalid Codex history boundary')
    rollout_id = base.get('thread_id')
    if not isinstance(rollout_id, str) or str(uuid.UUID(rollout_id)) != rollout_id:
        raise ValueError('Invalid Codex rollout identifier')
    return HistoryBase(rollout_id, ordinal, offset)


def rollout_roots(home):
    return tuple((Path(home) / name).resolve() for name in ('sessions', 'archived_sessions'))


def managed_rollout(candidate, roots):
    if not candidate:
        return None
    try:
        path = Path(candidate).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if any(path.is_relative_to(root) for root in roots) and path.is_file() and path.name.endswith(('.jsonl', '.jsonl.zst', '.jsonl.gz')):
        return path
    return None


def rollout_identity(path):
    match = ROLLOUT_NAME.fullmatch(path.name)
    if match:
        owner, identifier = match.groups()
        return owner, identifier or owner
    return None, None


@lru_cache(maxsize=128)
def find_rollouts(roots, rollout_id, cache_period):
    return tuple(path for root in roots for path in root.rglob(f'*{rollout_id}.jsonl*')
                 if rollout_identity(path)[1] == rollout_id)


def ancestor_candidates(roots, lookup, rollout_id):
    current = managed_rollout(lookup(rollout_id), roots)
    if current and rollout_identity(current)[1] == rollout_id:
        yield current
    for path in find_rollouts(roots, rollout_id, int(time.monotonic() // 30)):
        if path != current:
            yield path


@contextmanager
def open_rollout(path):
    if path.suffix == '.gz':
        with gzip.open(path, 'rb') as source:
            yield source
    elif path.suffix == '.zst':
        try:
            from compression import zstd  # Python 3.14+.
        except ImportError:
            zstd = None
        if zstd is not None:
            with zstd.open(path, 'rb') as source:
                yield source
            return
        binary = shutil.which('zstd') or next((str(p) for p in (Path('/opt/homebrew/bin/zstd'), Path('/usr/local/bin/zstd'))
                                               if p.is_file() and os.access(p, os.X_OK)), None)
        if not binary:
            raise FileNotFoundError('zstd is needed to read this archived Codex rollout')
        with subprocess.Popen([binary, '-dc', '--', str(path)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
            try:
                yield process.stdout
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
    else:
        with path.open('rb') as source:
            yield source


def read_metadata(source, thread_id):
    line = source.readline(MAX_RECORD)
    if not line.endswith(b'\n'):
        raise ValueError('Incomplete Codex rollout header')
    session = json.loads(line)
    metadata = session.get('payload') if isinstance(session, dict) and session.get('type') == 'session_meta' else None
    if not isinstance(metadata, dict) or metadata.get('id') != thread_id:
        raise ValueError('Codex rollout belongs to a different thread')
    return line, metadata


def inherited_history(roots, lookup, leaf_path, leaf_id, base):
    """Open each ancestor at the exact bookmark recorded when the branch was made."""
    parts, seen = [], {leaf_path}
    while base is not None:
        if len(parts) >= MAX_ANCESTORS:
            raise ValueError('Codex history lineage is too deep')
        for candidate in ancestor_candidates(roots, lookup, base.rollout_id):
            path = managed_rollout(candidate, roots)
            if not path or path in seen or rollout_identity(path)[1] != base.rollout_id:
                continue
            try:
                stat = path.stat()
                if path.suffix == '.jsonl' and stat.st_size < base.offset:
                    continue
                owner, _ = rollout_identity(path)
                # Native rollouts append after a fork bookmark; that suffix is outside this view.
                generation = (stat.st_dev, stat.st_ino, stat.st_mtime_ns if path.suffix != '.jsonl' else 0)
                ancestor, part = read_ancestor(path, owner, base, generation)
            except (OSError, ValueError, EOFError, LookupError):
                continue
            seen.add(path)
            parts.append(part)
            base = ancestor
            break
        else:
            # Do not look up archived history by a similarly named session or title.
            raise FileNotFoundError('Codex history source is not available')
    messages, turns = {}, {}
    for part in reversed(parts):
        for message in part.messages:
            messages[message['itemId']] = {**message, 'threadId': leaf_id, 'clientId': None}
        for turn_id, turn in part.turns.items():
            turns[turn_id] = {**turns.get(turn_id, {}), **turn}
    return messages, turns


@lru_cache(maxsize=16)
def read_ancestor(path, owner, end, generation):
    reader = RolloutReader(path, owner)
    with open_rollout(path) as source:
        header, metadata = read_metadata(source, owner)
        base = history_base(metadata)
        if metadata.get('history_mode', 'paginated') != 'paginated' or metadata.get('attached_thread'):
            raise ValueError('Invalid ancestor for visible Codex history')
        reader.ordinal = base.ordinal + 1 if base else 1
        reader.offset = len(header)
        if reader.offset > end.offset or reader.ordinal >= end.ordinal:
            raise ValueError('Codex history boundary precedes its source')
        reader._read(source, end.offset, end.ordinal)
        if reader.offset != end.offset:
            raise ValueError('Codex history boundary is outside a complete record')
        return base, RolloutTail(0, tuple(reader.messages.values()), reader.turns)


@lru_cache(maxsize=8)
def rollout_reader(path, thread_id):
    return RolloutReader(path, thread_id)


class RolloutReader:
    def __init__(self, path, thread_id):
        self.path = Path(path)
        self.thread_id = thread_id
        self.lock = threading.Lock()
        self.generation = None
        self.offset = 0
        self.anchor = b''
        self.ordinal = 0
        self.messages = {}
        self.turns = {}

    def tail(self, checkpoint):
        if self.path.suffix != '.jsonl':
            with open_rollout(self.path) as source:
                _, metadata = read_metadata(source, self.thread_id)
                base = history_base(metadata)
                reader = RolloutReader(self.path, self.thread_id)
                reader.ordinal = base.ordinal + 1 if base else 1
                reader._read(source)
                return metadata, RolloutTail(base.ordinal if base else 0, tuple(reader.messages.values()), reader.turns)
        with self.lock, self.path.open('rb') as source:
            stat = os.fstat(source.fileno())
            header, metadata = read_metadata(source, self.thread_id)
            base = history_base(metadata)
            if base is not None:
                start, cutoff = 0, base.ordinal
            elif checkpoint is not None:
                start, cutoff = checkpoint
            elif metadata.get('history_mode') == 'paginated':
                start, cutoff = 0, 0
            else:
                return None
            if (type(cutoff) is not int or cutoff < 0 or type(start) is not int or
                    not 0 <= start <= stat.st_size):
                return None
            generation = (stat.st_dev, stat.st_ino, start, cutoff, header)
            if not self._same_rollout(source, stat.st_size, generation):
                self.generation = generation
                self.offset = start
                self.ordinal = cutoff
                self.messages.clear()
                self.turns.clear()
            source.seek(self.offset)
            self._read(source, stat.st_size)
            source.seek(max(0, self.offset - 128))
            self.anchor = source.read(min(128, self.offset))
            return metadata, RolloutTail(cutoff, tuple(self.messages.values()), dict(self.turns))

    def _read(self, source, end=None, end_ordinal=None):
        while end is None or self.offset < end:
            maximum = MAX_RECORD if end is None else min(MAX_RECORD, end - self.offset)
            line = source.readline(maximum)
            length = len(line)
            if not line.endswith(b'\n'):
                if length < MAX_RECORD:
                    break  # Ignore an incomplete record at the end of a file or bookmark.
                while line and not line.endswith(b'\n') and (end is None or self.offset + length < end):
                    maximum = MAX_RECORD if end is None else min(MAX_RECORD, end - self.offset - length)
                    line = source.readline(maximum)
                    length += len(line)
                if not line.endswith(b'\n'):
                    break
            else:
                try:
                    row = json.loads(line)
                    ordinal = row.get('ordinal') if isinstance(row, dict) else None
                    if type(ordinal) is int:
                        self.ordinal = max(self.ordinal, ordinal)
                    if end_ordinal is not None and self.ordinal >= end_ordinal:
                        raise LookupError('Codex history boundary does not match its ordinal')
                    self._event(row)
                except (ValueError, UnicodeError):
                    pass
            self.offset += length
            self.ordinal += 1

    def _same_rollout(self, source, size, generation):
        if self.generation != generation or self.offset > size:
            return False
        source.seek(self.offset - len(self.anchor))
        return source.read(len(self.anchor)) == self.anchor

    def _event(self, row):
        if not isinstance(row, dict) or row.get('type') != 'event_msg':
            return
        event = row.get('payload')
        if not isinstance(event, dict) or event.get('thread_id', self.thread_id) != self.thread_id:
            return
        turn_id, kind = event.get('turn_id'), event.get('type')
        if not isinstance(turn_id, str) or not turn_id or not isinstance(kind, str):
            return
        if kind in TURN_STATUSES:
            changes = {'status': TURN_STATUSES[kind]}
            for key in ('started_at', 'completed_at'):
                if type(event.get(key)) is int:
                    changes[key] = event[key]
            recorded_at = timestamp_ms(row.get('timestamp'))
            if recorded_at:
                time_key = 'started_at' if changes['status'] == 'inProgress' else 'completed_at'
                changes.setdefault(time_key, recorded_at // 1000)
            self._turn(turn_id, **changes)
        elif kind == 'item_completed':
            self._message(turn_id, event, row.get('timestamp'))

    def _turn(self, turn_id, **changes):
        previous = self.turns.get(turn_id, {'turn_id': turn_id, 'rollout_ordinal': self.ordinal})
        self.turns[turn_id] = {**previous, **changes}

    def _message(self, turn_id, event, timestamp):
        item = event.get('item')
        if not isinstance(item, dict) or not isinstance(item.get('type'), str) or item['type'] not in MESSAGE_ROLES:
            return
        item_id = item.get('id')
        if not isinstance(item_id, str) or not item_id or len(item_id) > 256:
            return
        content = item.get('content')
        text = item.get('text')
        if not isinstance(text, str):
            text = '\n'.join(part['text'] for part in (content if isinstance(content, list) else [])
                             if isinstance(part, dict) and part.get('type') in ('Text', 'text', 'input_text', 'output_text')
                             and isinstance(part.get('text'), str))
        phase = item.get('phase')
        role = MESSAGE_ROLES[item['type']]
        if role == 'assistant' and phase in ('final', 'final_answer'):
            self._turn(turn_id, final_agent_item_id=item_id)
        created = next((event[k] for k in ('started_at_ms', 'completed_at_ms') if type(event.get(k)) is int), None)
        previous = self.messages.get(item_id)
        self.messages[item_id] = {'threadId': self.thread_id, 'turnId': turn_id, 'itemId': item_id,
            'createdAtMs': created if created is not None else timestamp_ms(timestamp),
            'ordinal': previous['ordinal'] if previous else self.ordinal,
            'clientId': item.get('client_id', item.get('clientId')), 'role': role, 'phase': phase, 'text': text}
