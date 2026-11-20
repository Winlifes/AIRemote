"""Persist local Claude controls and decode goal events from its transcript."""

import json


MODELS = ('', 'haiku', 'sonnet', 'opus', 'fable')
EFFORTS = ('', 'low', 'medium', 'high', 'xhigh', 'max')
MODES = ('manual', 'plan')
DEFAULT_SETTINGS = {'model': '', 'effort': '', 'mode': 'manual'}


def normalize_settings(value):
    choices = {'model': MODELS, 'effort': EFFORTS, 'mode': MODES}
    if set(value) != set(choices) or any(value[key] not in allowed for key, allowed in choices.items()):
        raise ValueError('请选择支持的 Claude 模型、思考强度和权限模式。')
    return dict(value)


def goal_event(entry):
    attachment = entry.get('attachment')
    if entry.get('type') == 'attachment' and isinstance(attachment, dict) and attachment.get('type') == 'goal_status':
        objective = attachment.get('condition')
        if isinstance(objective, str) and objective.strip() and len(objective) <= 4000:
            status = 'complete' if attachment.get('met') is True else 'blocked' if attachment.get('impossible') is True else 'active'
            def nonnegative(field):
                value = attachment.get(field)
                return max(0, value) if type(value) is int else 0
            return {'objective': objective, 'status': status, 'tokensUsed': nonnegative('tokens'),
                    'timeUsedSeconds': nonnegative('durationMs') // 1000}
    command = entry.get('commandRun')
    content = entry.get('content')
    if (entry.get('type') == 'system' and entry.get('subtype') == 'local_command' and
            isinstance(command, dict) and command.get('command') == 'goal' and isinstance(content, str) and
            content.startswith(('<local-command-stdout>Goal cleared:', '<local-command-stdout>No goal set'))):
        return {}
    return None


class ClaudeState:
    def __init__(self, journal):
        self.journal = journal
        with journal.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS claude_queue(
                    id TEXT PRIMARY KEY, thread TEXT NOT NULL, text TEXT NOT NULL, position INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS claude_queue_thread ON claude_queue(thread,position);
                CREATE TABLE IF NOT EXISTS claude_settings(thread TEXT PRIMARY KEY, options TEXT NOT NULL);
            ''')

    def pending_threads(self):
        with self.journal.connect() as db:
            return [row[0] for row in db.execute('SELECT thread FROM claude_queue GROUP BY thread ORDER BY min(rowid)')]

    def settings(self, tid):
        with self.journal.connect() as db:
            row = db.execute('SELECT options FROM claude_settings WHERE thread=?', (tid,)).fetchone()
        return normalize_settings(json.loads(row[0])) if row else dict(DEFAULT_SETTINGS)

    def save_settings(self, db, tid, value):
        value = normalize_settings(value)
        db.execute('INSERT INTO claude_settings VALUES(?,?) ON CONFLICT(thread) DO UPDATE SET options=excluded.options',
                   (tid, json.dumps(value)))
        return value

    def rows(self, db, tid):
        return db.execute('SELECT id,text FROM claude_queue WHERE thread=? ORDER BY position,rowid', (tid,)).fetchall()

    def items(self, db, tid):
        return [{'id': row['id'], 'input': [{'type': 'text', 'text': row['text']}]} for row in self.rows(db, tid)]

    def reorder(self, db, tid, ids):
        db.executemany('UPDATE claude_queue SET position=? WHERE thread=? AND id=?',
                       ((index, tid, identifier) for index, identifier in enumerate(ids)))

    def remove(self, db, tid, ids):
        for identifier in ids:
            db.execute('DELETE FROM claude_queue WHERE thread=? AND id=?', (tid, identifier))
            self.journal.finish(identifier, 'cancelled', {'threadId': tid, 'executionState': 'cancelled'}, connection=db)
