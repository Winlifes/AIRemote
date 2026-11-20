"""Read the unprojected part of a Codex paginated rollout."""

from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import threading


MAX_RECORD = 16 * 1024 * 1024
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
        with self.lock, self.path.open('rb') as source:
            stat = os.fstat(source.fileno())
            header = source.readline(MAX_RECORD)
            if not header.endswith(b'\n'):
                return None
            session = json.loads(header)
            if not isinstance(session, dict) or session.get('type') != 'session_meta':
                return None
            metadata = session.get('payload')
            if not isinstance(metadata, dict) or metadata.get('id') != self.thread_id:
                return None
            base = metadata.get('history_base')
            if base is not None:
                # Compacted rollouts replace everything from this ordinal onward.
                if not isinstance(base, dict) or base.get('thread_id') != self.thread_id:
                    return None
                start, cutoff = 0, base.get('end_ordinal_exclusive')
            elif checkpoint is not None:
                start, cutoff = checkpoint
            else:
                return None
            if (type(cutoff) is not int or cutoff < 0 or type(start) is not int or
                    not 0 <= start <= stat.st_size):
                return None
            generation = (stat.st_dev, stat.st_ino, start, cutoff, header[:128])
            if not self._same_rollout(source, stat.st_size, generation):
                self.generation = generation
                self.offset = start
                self.ordinal = cutoff
                self.messages.clear()
                self.turns.clear()
            source.seek(self.offset)
            while source.tell() < stat.st_size:
                line = source.readline(MAX_RECORD)
                if not line.endswith(b'\n'):
                    if len(line) < MAX_RECORD:
                        break  # The writer has not committed this JSONL record yet.
                    while line and not line.endswith(b'\n'):
                        line = source.readline(MAX_RECORD)
                    if not line:
                        break
                else:
                    try:
                        self._event(json.loads(line))
                    except (ValueError, UnicodeError):
                        pass
                self.offset = source.tell()
                self.ordinal += 1
            source.seek(max(0, self.offset - 128))
            self.anchor = source.read(min(128, self.offset))
            return RolloutTail(cutoff, tuple(self.messages.values()), dict(self.turns))

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
