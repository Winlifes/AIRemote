import base64
import gzip
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import project_catalog
from codex_probe import History, connect_read_only
from project_catalog import ProjectCatalog

THREAD = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
PROJECT = '33333333-3333-4333-8333-333333333333'
PROJECT_B = '44444444-4444-4444-8444-444444444444'


class LazyReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.a = self.root / 'A'; self.a.mkdir()
        self.b = self.root / 'B'; self.b.mkdir()
        self.state = self.root / 'state_5.sqlite'
        with sqlite3.connect(self.state) as db:
            db.executescript('''
              CREATE TABLE projects(id TEXT,name TEXT,position INTEGER);
              CREATE TABLE project_roots(project_id TEXT,position INTEGER,path TEXT);
              CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,project_id TEXT,history_mode TEXT,
                archived INTEGER,updated_at INTEGER,first_user_message TEXT,preview TEXT);
            ''')
            db.executemany('INSERT INTO projects VALUES(?,?,?)', [(PROJECT, 'A', 0), (PROJECT_B, 'B', 1)])
            db.executemany('INSERT INTO project_roots VALUES(?,?,?)', [(PROJECT, 0, str(self.a)), (PROJECT_B, 0, str(self.b))])
            db.executemany('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?)', [
                (THREAD, None, 'A chat', str(self.a), PROJECT, 'paginated', 0, 100, 'DO NOT FETCH THIS', 'DO NOT FETCH THIS'),
                (OTHER, None, 'B chat', str(self.b), PROJECT_B, 'paginated', 0, 200, 'PRIVATE B MESSAGE', 'PRIVATE B MESSAGE'),
                ('55555555-5555-4555-8555-555555555555', None, 'A connector chat', str(self.a / 'subdir'), None, 'paginated', 0, 300, 'PRIVATE', 'PRIVATE'),
                ('66666666-6666-4666-8666-666666666666', None, 'Archived A', str(self.a), PROJECT, 'paginated', 1, 400, 'PRIVATE', 'PRIVATE'),
            ])
        self.catalog = ProjectCatalog([self.a], include_desktop=True, codex_home=self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_project_index_does_not_touch_chats_or_history(self):
        queries = []
        def traced(path):
            db = connect_read_only(path); db.set_trace_callback(queries.append); return db
        with patch.object(project_catalog, 'connect_read_only', traced):
            projects = self.catalog.projects()
        self.assertEqual([p['name'] for p in projects], ['A', 'B'])
        self.assertFalse(any('threads' in query.lower() or 'thread_items' in query.lower() for query in queries))
        self.assertFalse((self.root / 'thread_history_1.sqlite').exists())

    def test_expanded_project_returns_only_titles_and_members(self):
        chats = self.catalog.threads(PROJECT)
        self.assertEqual([c['title'] for c in chats], ['A connector chat', 'A chat'])
        self.assertTrue(all(c['projectId'] == PROJECT for c in chats))
        self.assertNotIn('DO NOT FETCH', json.dumps(chats))
        self.assertNotIn('PRIVATE B MESSAGE', json.dumps(chats))
        self.assertTrue(all(not c['turns'] and c['runtimeStatus'] == 'unknown' for c in chats))

    def test_native_project_membership_and_static_allowlist(self):
        self.assertEqual(self.catalog.project_for_thread(OTHER), PROJECT_B)
        self.assertEqual(len(self.catalog.threads(PROJECT, allowed_ids={THREAD})), 1)
        with self.assertRaises(LookupError): self.catalog.threads('77777777-7777-4777-8777-777777777777')

    def test_project_catchall_hides_spawned_and_legacy_subagents(self):
        edge, legacy, tagged, normal = [str(n) * 8 + '-1111-4111-8111-111111111111' for n in (7, 8, 9, 6)]
        with sqlite3.connect(self.state) as db:
            db.executescript('''ALTER TABLE threads ADD COLUMN source TEXT;
                ALTER TABLE threads ADD COLUMN thread_source TEXT;
                CREATE TABLE thread_spawn_edges(parent_thread_id TEXT, child_thread_id TEXT, status TEXT);''')
            db.execute('INSERT INTO thread_spawn_edges VALUES (?,?,?)', (THREAD, edge, 'closed'))
            for tid, source, classification in [
                (edge, 'vscode', None),
                (legacy, json.dumps({'subagent': {'thread_spawn': {'parent_thread_id': THREAD}}}), None),
                (tagged, 'unknown', 'subagent'), (normal, 'appServer', 'user'),
            ]:
                db.execute('INSERT INTO threads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (tid, None, 'Same title', str(self.a), PROJECT, 'paginated', 0, 500, '', '', source, classification))
        visible = {chat['threadId'] for chat in self.catalog.threads(PROJECT)}
        self.assertTrue({normal, THREAD} <= visible)
        self.assertFalse({edge, legacy, tagged} & visible)
        self.assertEqual(self.catalog.threads(PROJECT, allowed_ids={edge, legacy, tagged}), [])

    def test_moved_legacy_chat_follows_native_project_membership(self):
        legacy_id = '77777777-7777-4777-8777-777777777777'
        (self.root / '.codex-global-state.json').write_text(json.dumps({
            'app-server-project-id-by-legacy-project-id-by-host': {'local:' + str(self.root): {legacy_id: PROJECT_B}},
            'thread-project-assignments': {THREAD: {'projectKind': 'local', 'projectId': legacy_id}}
        }))
        with sqlite3.connect(self.state) as db:
            db.execute('UPDATE threads SET project_id=NULL WHERE id=?', (THREAD,))
        self.assertEqual(self.catalog.project_for_thread(THREAD), PROJECT_B)
        self.assertIn(THREAD, {chat['threadId'] for chat in self.catalog.threads(PROJECT_B)})
        self.assertNotIn(THREAD, {chat['threadId'] for chat in self.catalog.threads(PROJECT)})

    def make_history(self):
        path = self.root / 'thread_history_1.sqlite'
        with sqlite3.connect(path) as db:
            db.executescript('''
              CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,rollout_ordinal INTEGER,
                created_at_ms INTEGER,item_json TEXT,item_type TEXT);
              CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,rollout_ordinal INTEGER,status TEXT,
                started_at INTEGER,completed_at INTEGER,final_agent_item_id TEXT);
            ''')
            for index in range(120):
                turn = 'turn-' + str(index // 2)
                item = {'type': 'userMessage', 'clientId': 'client-' + str(index), 'content': [{'type': 'text', 'text': str(index)}]} if index % 2 == 0 else {'type': 'agentMessage', 'phase': 'final_answer', 'text': str(index)}
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)', (THREAD, turn, 'item-%03d' % index, index, index * 1000, json.dumps(item), item['type']))
                if index % 2 == 0:
                    db.execute('INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)', (THREAD, turn, index, 'completed', 100 + index * 10, 107 + index * 10, 'final'))
            db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)', (OTHER, 'other', 'private', 9999, 0, json.dumps({'type': 'agentMessage', 'text': 'PRIVATE B MESSAGE'}), 'agentMessage'))
        return History(THREAD, [self.root], codex_home=self.root)

    def test_initial_messages_are_one_recent_page(self):
        history = self.make_history(); page = history.message_page(40)
        self.assertEqual([m['ordinal'] for m in page['messages']], list(range(80, 120)))
        self.assertTrue(page['hasMore'])
        self.assertEqual(len(page['turns']), 20)
        self.assertTrue(all(t['completed_at'] - t['started_at'] == 7 for t in page['turns']))
        self.assertNotIn('PRIVATE B MESSAGE', json.dumps(page))

    def test_expanded_chat_status_reads_only_latest_turn_metadata(self):
        self.make_history()
        with sqlite3.connect(self.root / 'thread_history_1.sqlite') as db:
            db.execute("UPDATE thread_turns SET status='inProgress',completed_at=NULL WHERE thread_id=? AND turn_id='turn-59'", (THREAD,))
        queries=[]
        def traced(path):
            db=connect_read_only(path);db.set_trace_callback(queries.append);return db
        chats=self.catalog.threads(PROJECT,allowed_ids={THREAD})
        with patch.object(project_catalog,'connect_read_only',traced):
            running=self.catalog.attach_turn_status(chats)
        self.assertEqual(running[0]['runtimeStatus'],'active')
        self.assertFalse(any('thread_items' in q.lower() or OTHER in q for q in queries))
        with sqlite3.connect(self.root/'thread_history_1.sqlite') as db:
            db.execute("UPDATE thread_turns SET status='completed',completed_at=999 WHERE thread_id=? AND turn_id='turn-59'",(THREAD,))
        self.assertEqual(self.catalog.attach_turn_status(chats)[0]['runtimeStatus'],'idle')

    def test_upward_pages_have_no_gaps_or_duplicates(self):
        history = self.make_history()
        recent = history.message_page(40)
        middle = history.message_page(40, recent['olderCursor'])
        oldest = history.message_page(40, middle['olderCursor'])
        self.assertEqual([m['ordinal'] for m in oldest['messages'] + middle['messages'] + recent['messages']], list(range(120)))
        self.assertFalse(oldest['hasMore']); self.assertIsNone(oldest['olderCursor'])

    def test_cursor_is_bound_to_chat_and_handles_equal_ordinals(self):
        history = self.make_history()
        with sqlite3.connect(history.history_path) as db:
            for key in ['tie-a', 'tie-b']:
                db.execute('INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)', (THREAD, 'turn-59', key, 120, 120000, json.dumps({'type': 'agentMessage', 'text': key}), 'agentMessage'))
        one = history.message_page(1)
        two = history.message_page(1, one['olderCursor'])
        self.assertEqual([one['messages'][0]['itemId'], two['messages'][0]['itemId']], ['tie-b', 'tie-a'])
        other = History(OTHER, [self.root], codex_home=self.root)
        with self.assertRaises(ValueError): other.message_page(40, one['olderCursor'])
        for cursor in ['invalid', base64.urlsafe_b64encode(b'[]').decode().rstrip('='), base64.urlsafe_b64encode(json.dumps({'v': 1, 't': THREAD, 'o': '1 OR 1=1', 'i': 'item'}).encode()).decode().rstrip('=')]:
            with self.assertRaises(ValueError): history.message_page(40, cursor)

    def test_polling_recent_page_sees_streamed_item_updates(self):
        history = self.make_history(); before = history.message_page(40)
        with sqlite3.connect(history.history_path) as db:
            db.execute('UPDATE thread_items SET item_json=? WHERE thread_id=? AND item_id=?', (json.dumps({'type': 'agentMessage', 'text': 'streamed update'}), THREAD, 'item-119'))
        after = history.message_page(40)
        self.assertEqual(before['messages'][-1]['itemId'], after['messages'][-1]['itemId'])
        self.assertEqual(after['messages'][-1]['text'], 'streamed update')

    def write_rollout(self, history, events, base=None):
        sessions = self.root / 'sessions'
        sessions.mkdir(exist_ok=True)
        path = sessions / 'rollout-test.jsonl'
        metadata = {'id': THREAD}
        if base is not None:
            metadata['history_base'] = {'thread_id': THREAD, 'end_ordinal_exclusive': base, 'end_byte_offset': 80000}
        header = json.dumps({'type': 'session_meta', 'payload': metadata}).encode() + b'\n'
        path.write_bytes(header + b''.join(self.rollout_event(e) for e in events))
        with sqlite3.connect(history.state_path) as db:
            db.execute('ALTER TABLE threads ADD COLUMN rollout_path TEXT')
            db.execute('UPDATE threads SET rollout_path=? WHERE id=?', (str(path), THREAD))
        with sqlite3.connect(history.history_path) as db:
            db.execute('CREATE TABLE thread_history_projection_state(thread_id TEXT, next_rollout_byte_offset INTEGER, next_rollout_ordinal INTEGER)')
            db.execute('INSERT INTO thread_history_projection_state VALUES (?,?,?)', (THREAD, 90000 if base is not None else len(header), 120))
        return path

    @staticmethod
    def rollout_event(event):
        return json.dumps({'type': 'event_msg', 'timestamp': '2026-11-20T18:30:01Z', 'payload': event}).encode() + b'\n'

    def test_compacted_segment_replaces_old_branch_and_paginates_into_sqlite(self):
        history = self.make_history()
        turn_id = 'replacement'
        event = {'thread_id': THREAD, 'turn_id': turn_id}
        self.write_rollout(history, [
            {**event, 'type': 'task_started', 'started_at': 1200},
            {**event, 'type': 'item_completed', 'started_at_ms': 1200000, 'item': {
                'type': 'UserMessage', 'id': 'current-user', 'client_id': 'mobile-client',
                'content': [{'type': 'text', 'text': 'current question'}]}},
            {**event, 'type': 'item_completed', 'item': {'type': 'CommandExecution', 'id': 'tool', 'text': 'private tool output'}},
            {**event, 'type': 'item_completed', 'thread_id': OTHER, 'item': {
                'type': 'AgentMessage', 'id': 'other-result', 'text': 'another thread'}},
            {**event, 'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'current-progress',
                'phase': 'commentary', 'content': [{'type': 'Text', 'text': 'current progress'}]}},
            {**event, 'type': 'item_completed', 'item': {'type': 'AgentMessage', 'id': 'current-final',
                'phase': 'final_answer', 'content': [{'type': 'Text', 'text': 'current reply'}]}},
            {**event, 'type': 'task_complete', 'completed_at': 1207},
        ], base=118)
        page = history.message_page(4)
        self.assertEqual([m['itemId'] for m in page['messages']], ['item-117', 'current-user', 'current-progress', 'current-final'])
        self.assertEqual([m['text'] for m in page['messages'][-2:]], ['current progress', 'current reply'])
        older = history.message_page(4, page['olderCursor'])
        self.assertEqual([m['ordinal'] for m in older['messages']], [113, 114, 115, 116])
        self.assertEqual(history.snapshot()['turns'][0], {'turn_id': turn_id, 'status': 'completed',
            'started_at': 1200, 'completed_at': 1207, 'final_agent_item_id': 'current-final'})
        receipt = history.delivery_receipt('mobile-client')
        self.assertEqual(receipt['turnStatus'], 'completed')
        self.assertEqual([m['itemId'] for m in receipt['laterAssistantMessages']], ['current-progress', 'current-final'])
        self.assertFalse(history.delivery_receipt('client-118')['receivedInHistory'])

    def test_unprojected_tail_resumes_after_a_partial_record(self):
        history = self.make_history()
        turn = {'thread_id': THREAD, 'turn_id': 'turn-59'}
        path = self.write_rollout(history, [{**turn, 'type': 'task_complete', 'completed_at': 9999}])
        item = self.rollout_event({**turn, 'type': 'item_completed', 'item': {
            'type': 'AgentMessage', 'id': 'appended', 'text': 'new text', 'phase': 'final_answer'}})
        with path.open('ab') as out:
            out.write(item[:-1])
        self.assertEqual(history.message_page(1)['messages'][0]['itemId'], 'item-119')
        with path.open('ab') as out:
            out.write(item[-1:])
        newest = history.message_page(1)
        self.assertEqual(newest['messages'][0]['itemId'], 'appended')
        self.assertEqual(newest['turns'][0]['completed_at'], 9999)
        self.assertEqual(history.message_page(1, newest['olderCursor'])['messages'][0]['itemId'], 'item-119')

    def test_fork_reads_frozen_ancestors_including_a_second_revert(self):
        history = self.make_history()
        sessions = self.root / 'sessions'
        archives = self.root / 'archived_sessions'
        archives.mkdir()
        intermediate_id = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'

        def item(owner, identifier, client=None):
            return {'type': 'item_completed', 'thread_id': owner, 'turn_id': identifier, 'item': {
                'type': 'UserMessage', 'id': identifier, 'client_id': client,
                'content': [{'type': 'text', 'text': identifier}]}}

        def header(owner, base=None):
            metadata = {'id': owner, 'history_mode': 'paginated'}
            if base:
                metadata['history_base'] = dict(zip(('thread_id', 'end_ordinal_exclusive', 'end_byte_offset'), base))
            return json.dumps({'type': 'session_meta', 'payload': metadata}).encode() + b'\n'

        root_prefix = header(OTHER) + self.rollout_event(item(OTHER, 'before-fork', 'parent-client'))
        with gzip.open(archives / f'rollout-test-{OTHER}.jsonl.gz', 'wb') as out:
            out.write(root_prefix + self.rollout_event(item(OTHER, 'after-fork')))
        midpoint = header(THREAD, (OTHER, 2, len(root_prefix))) + self.rollout_event(item(THREAD, 'before-revert'))
        path = self.write_rollout(history, [], base=118)
        (sessions / f'rollout-test-{THREAD}_{intermediate_id}.jsonl').write_bytes(midpoint + self.rollout_event(item(THREAD, 'reverted')))
        path.write_bytes(header(THREAD, (intermediate_id, 4, len(midpoint))) + self.rollout_event(item(THREAD, 'after-revert', 'child-client')))

        page = history.message_page(2)
        self.assertEqual([m['itemId'] for m in page['messages']], ['before-revert', 'after-revert'])
        previous = history.message_page(2, page['olderCursor'])
        self.assertEqual([m['itemId'] for m in previous['messages']], ['before-fork'])
        self.assertEqual(previous['messages'][0]['threadId'], THREAD)
        self.assertFalse(history.delivery_receipt('parent-client')['receivedInHistory'])
        self.assertEqual(history.delivery_receipt('child-client')['userItemId'], 'after-revert')

    def test_missing_fork_archive_keeps_current_messages(self):
        history = self.make_history()
        path = self.write_rollout(history, [{'type': 'item_completed', 'turn_id': 'latest', 'item': {
            'type': 'AgentMessage', 'id': 'visible-leaf', 'text': 'after fork'}}], base=118)
        header, tail = path.read_bytes().split(b'\n', 1)
        data = json.loads(header)
        data['payload']['history_base']['thread_id'] = OTHER
        path.write_bytes(json.dumps(data).encode() + b'\n' + tail)
        self.assertEqual(history.message_page(2)['messages'][-1]['itemId'], 'visible-leaf')

    def test_receipt_targets_one_client_message_without_loading_history(self):
        history = self.make_history()
        with patch.object(history, 'messages', side_effect=AssertionError('Must not load all messages')):
            receipt = history.delivery_receipt('client-80')
        self.assertTrue(receipt['receivedInHistory'])
        self.assertEqual(receipt['userItemId'], 'item-080')
        self.assertEqual([m['itemId'] for m in receipt['laterAssistantMessages']], ['item-081'])
        self.assertFalse(history.delivery_receipt('missing-client')['receivedInHistory'])


if __name__ == '__main__': unittest.main()
