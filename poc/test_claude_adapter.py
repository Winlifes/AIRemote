import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from claude_adapter import ClaudeAdapter, ClaudeCLI, ClaudeError, project_id
from control_server import Journal


class LocalCLI:
    def __init__(self, session_id):
        self.current = {session_id: {'status': 'working'}}
        self.mode = 'manual'

    def live(self, fresh=False):
        return self.current

    def invalidate_live(self):
        pass

    def write_mode(self):
        return self.mode

    def supports_goals(self):
        return True


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
        self.root = root
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
        session = self.adapter.session(self.tid)
        self.assertEqual(session['runtimeStatus']['type'], 'idle')
        self.assertFalse(session['writeAvailable'])
        self.cli.mode = 'manual'
        self.assertTrue(self.adapter.session(self.tid)['writeAvailable'])

    def test_only_authorized_files_linked_by_claude_can_be_downloaded(self):
        file, = self.adapter.artifacts(self.tid)
        self.assertEqual(file['name'], 'report.txt')
        self.assertEqual(self.adapter.read_artifact(self.tid, file['id'])[1], b'Published report')
        with self.assertRaises(LookupError):
            self.adapter.read_artifact(self.tid, '0' * 24)

    def test_pending_queue_is_editable_idempotent_and_paused_after_restart(self):
        def submit(key, kind, **changes):
            return self.adapter.perform(key, kind, {'threadId': self.tid, **changes})
        with self.assertRaisesRegex(ClaudeError, '释放 Claude Code'):
            submit('native-owner', 'queue_add', text='Guard the desktop owner')
        self.cli.current = {}
        self.adapter.paused.add(self.tid)
        first = submit('first-prompt', 'queue_add', text='First')
        second = submit('second-prompt', 'queue_add', text='Second')
        self.assertEqual(first['requestStatus'], 'queued')
        self.assertFalse(first['delivery']['receivedInHistory'])
        self.assertEqual(first, submit('first-prompt', 'queue_add', text='First'))
        ids = [second['operationId'], first['operationId']]
        submit('edit-first', 'queue_update', itemId=ids[1], text='Edited')
        submit('priority-order', 'queue_reorder', ids=ids)
        submit('save-options', 'settings', model='haiku', effort='low', mode='plan')
        self.adapter.close()
        self.adapter = ClaudeAdapter(Journal(self.root / 'journal'), [self.work], [self.work], home=self.root, cli=self.cli)
        self.assertEqual([(q['id'], q['input'][0]['text']) for q in self.adapter.queue(self.tid)],
                         [(ids[0], 'Second'), (ids[1], 'Edited')])
        self.assertTrue(self.adapter.session(self.tid)['queuePaused'])
        self.assertEqual(self.adapter.session(self.tid)['claudeSettings']['mode'], 'plan')
        with self.assertRaises(ClaudeError):
            submit('stale-clear', 'queue_clear', ids=[ids[0]])
        submit('clear-queue', 'queue_clear', ids=ids)
        self.assertEqual(self.adapter.queue(self.tid), [])
        removed = self.adapter.receipt(self.adapter.journal.get(ids[0]))
        self.assertEqual(removed['requestStatus'], 'cancelled')
        self.assertFalse(removed['delivery']['receivedInHistory'])

    def test_transcript_goal_status_and_stop_keep_desktop_read_only(self):
        path = self.adapter.row(self.tid).log
        with path.open('ab') as output:
            output.write(b'\n')
            value = {'type': 'attachment', 'sessionId': self.tid, 'uuid': str(uuid4()),
                     'attachment': {'type': 'goal_status', 'sentinel': True, 'met': False, 'condition': 'Check the report'}}
            output.write((json.dumps(value) + '\n').encode())
        self.assertEqual(self.adapter.goal(self.tid)['goal']['status'], 'active')
        self.assertFalse(self.adapter.goal(self.tid)['writable'])
        with self.assertRaisesRegex(ClaudeError, '指令已变化'):
            self.adapter.perform('stop-native', 'stop', {'threadId': self.tid, 'executionId': str(uuid4())})
        self.cli.current = {}
        self.assertEqual(self.adapter.goal(self.tid)['goal']['status'], 'paused')
        self.assertFalse(self.adapter.session(self.tid)['writeAvailable'])

    def test_claude_live_refresh_and_process_exit_discard_old_ownership(self):
        cli = ClaudeCLI(self.root)
        native = [{'sessionId': self.tid, 'status': 'working'}]
        cli.command = lambda _: json.dumps(native).encode()
        self.assertIn(self.tid, cli.live())
        native.clear()
        self.assertEqual(cli.live(fresh=True), {})
        self.assertEqual(cli.live(), {})
        native.append({'sessionId': self.tid, 'status': 'idle'})
        cli.invalidate_live()
        self.assertIn(self.tid, cli.live())


if __name__ == '__main__':
    unittest.main()
