import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from claude_adapter import ClaudeAdapter, ClaudeError, project_id
from control_server import Journal


class LocalCLI:
    def __init__(self, session_id):
        self.current = {session_id: {'status': 'working'}}
        self.mode = 'manual'

    def live(self, fresh=False):
        return self.current

    def write_mode(self):
        return self.mode


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.work = root / 'workspace'
        self.work.mkdir()
        self.report = self.work / 'report.txt'
        self.report.write_text('Published report')
        private = self.work / '.env'
        private.write_text('Private data')
        self.tid = str(uuid4())
        self.cli = LocalCLI(self.tid)
        journal = Journal(root / 'journal')
        self.adapter = ClaudeAdapter(journal, [self.work], [self.work], home=root, cli=self.cli)
        log = root / '.claude/projects/workspace' / (self.tid + '.jsonl')
        log.parent.mkdir(parents=True)
        self.first, self.second = str(uuid4()), str(uuid4())
        with log.open('w') as out:
            for uid, role, content, stop in [
                (self.first, 'user', 'First request', None),
                (str(uuid4()), 'assistant', 'Inspecting the file', 'tool_use'),
                (str(uuid4()), 'user', [{'type': 'tool_result', 'content': 'Internal output'}], None),
                (str(uuid4()), 'assistant', f'[Report]({self.report}) [Private]({private})', 'end_turn'),
                (self.second, 'user', 'Second request', None),
            ]:
                out.write(json.dumps({'type': role, 'sessionId': self.tid, 'uuid': uid, 'cwd': str(self.work),
                    'timestamp': '2026-01-01T01:00:00.000Z',
                    'message': {'role': role, 'content': content, 'stop_reason': stop}}) + '\n')
            out.write('{"type":"assistant"')

    def test_pages_keep_native_ids_and_skip_tool_records(self):
        recent = self.adapter.message_page(self.tid, 2)
        self.assertTrue(recent['hasMore'])
        self.assertEqual(recent['messages'][-1]['itemId'], self.second)
        self.assertEqual(recent['turns'][0]['status'], 'in_progress')
        self.assertEqual(recent['turns'][1]['status'], 'completed')
        older = self.adapter.message_page(self.tid, 2, recent['olderCursor'])
        self.assertFalse(older['hasMore'])
        self.assertEqual([m['text'] for m in older['messages']], ['First request', 'Inspecting the file'])
        self.assertEqual(older['messages'][1]['turnId'], self.first)

    def test_running_and_unknown_owner_states_guard_writes(self):
        session, = self.adapter.threads(project_id(self.work))
        self.assertEqual(session['runtimeStatus']['type'], 'active')
        self.assertFalse(session['writeAvailable'])
        with self.assertRaisesRegex(ClaudeError, '关闭会话'):
            self.adapter.perform('test-owner', 'send', {'threadId': self.tid, 'text': 'Continue'})
        self.cli.current = None
        self.assertIsNone(self.adapter.session(self.tid)['runtimeStatus'])
        self.assertFalse(self.adapter.session(self.tid)['writeAvailable'])
        self.cli.current = {}
        self.cli.mode = None
        self.assertFalse(self.adapter.session(self.tid)['writeAvailable'])
        self.cli.mode = 'manual'
        self.assertTrue(self.adapter.session(self.tid)['writeAvailable'])

    def test_only_authorized_files_linked_by_claude_can_be_downloaded(self):
        file, = self.adapter.artifacts(self.tid)
        self.assertEqual(file['name'], 'report.txt')
        self.assertEqual(self.adapter.read_artifact(self.tid, file['id'])[1], b'Published report')
        with self.assertRaises(LookupError):
            self.adapter.read_artifact(self.tid, '0' * 24)


if __name__ == '__main__':
    unittest.main()
