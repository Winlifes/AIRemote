"""Isolated stdio fixtures: no real Codex process, chat, or API account."""
import json
import os
from pathlib import Path
import plistlib
import queue
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from goal_transport import GoalOwnerClient, GoalUnavailable, GoalRPCError, checked_goal, goal_revision, validate_goal_rpc

THREAD = '11111111-1111-4111-8111-111111111111'
OTHER = '22222222-2222-4222-8222-222222222222'
PROXY = Path(__file__).with_name('goal_transport.py')
FAKE = '''
import json,sys
goal=None
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
    value=json.loads(line)
    method=value.get('method'); params=value.get('params',{})
    if method=='initialize':
        if params.get('reject'): send({'id':value['id'],'error':{'code':-32600,'message':'fixture rejected handshake'}})
        else: send({'id':value['id'],'result':{'echo':params}})
    elif method=='initialized': pass
    elif method=='thread/loaded/list': send({'id':value['id'],'result':{'data':['11111111-1111-4111-8111-111111111111'],'nextCursor':None}})
    elif method=='thread/goal/get': send({'id':value['id'],'result':{'goal':goal}})
    elif method=='thread/goal/set':
        goal=goal or {'threadId':params['threadId'],'objective':'','status':'paused','tokenBudget':None,'tokensUsed':0,'timeUsedSeconds':0,'createdAt':10,'updatedAt':10}
        goal.update({k:v for k,v in params.items() if k in {'objective','status','tokenBudget'}})
        send({'method':'thread/goal/updated','params':{'threadId':params['threadId'],'goal':goal}})
        send({'id':value['id'],'result':{'goal':goal}})
    elif method=='thread/goal/clear':
        goal=None
        send({'id':value['id'],'result':{'cleared':True}})
    elif method=='native-test':
        send({'method':'native-notification','params':{'text':'原样保留'}})
        send({'id':'native-approval','method':'item/commandExecution/requestApproval','params':{'command':'fixture-only'}})
        send({'id':value['id'],'result':params})
    elif value.get('id')=='native-approval': send({'method':'approval-observed','params':value})
'''


class GoalProxyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='arg-', dir='/tmp')
        self.root = Path(self.temp.name); self.root.chmod(0o700)
        fake = self.root / 'fake'; fake.write_text('#!' + sys.executable + '\n' + FAKE); fake.chmod(0o700)
        info = self.root / 'Info.plist'; info.write_bytes(plistlib.dumps({'CFBundleShortVersionString': '26.1111.21308'}))
        self.token = secrets.token_urlsafe(32)
        config = self.root / 'config.json'
        config.write_text(json.dumps({'token': self.token, 'executable': str(fake), 'bundleInfo': str(info), 'codexVersion': '26.1111.21308'})); config.chmod(0o600)
        self.process = subprocess.Popen([sys.executable, str(PROXY), '--config', str(config), '--', 'app-server'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.output = queue.Queue()
        self.reader = threading.Thread(target=lambda: [self.output.put(json.loads(line)) for line in self.process.stdout], daemon=True)
        self.reader.start()
        deadline = time.monotonic() + 3
        while not list(self.root.glob('s*.json')) and self.process.poll() is None and time.monotonic() < deadline:
            time.sleep(.01)
        self.client = GoalOwnerClient(self.root)

    def send(self, value):
        self.process.stdin.write(json.dumps(value, ensure_ascii=False).encode() + b'\n'); self.process.stdin.flush()

    def read(self):
        return self.output.get(timeout=3)

    def initialize(self, notification=True, request_id=93):
        params = {'clientInfo': {'name': 'native-fixture'}, 'capabilities': {'experimentalApi': True}}
        self.send({'id': request_id, 'method': 'initialize', 'params': params})
        self.assertEqual(self.read(), {'id': request_id, 'result': {'echo': params}})
        if notification:
            self.send({'method': 'initialized'})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            records = self.client._records()
            if records and self.client._call(records[0], 'status', {})['ready']:
                return
            time.sleep(.01)
        self.fail('Proxy did not observe native initialization')

    def tearDown(self):
        try:
            self.process.stdin.close()
            self.process.wait(timeout=5)
            self.reader.join(timeout=2)
            self.assertEqual(self.process.returncode, 0, self.process.stderr.read().decode())
            self.assertEqual(list(self.root.glob('s*.sock')), [])
            self.assertEqual(list(self.root.glob('s*.json')), [])
        finally:
            if self.process.poll() is None:
                self.process.kill(); self.process.wait()
            self.process.stdout.close(); self.process.stderr.close()
            self.temp.cleanup()

    def test_native_handshake_is_required_and_preserved(self):
        with self.assertRaises(GoalUnavailable):
            self.client.owner(THREAD)
        self.initialize()
        self.assertEqual(self.client.request('thread/goal/get', {'threadId': THREAD}), {'goal': None})

    def test_desktop_one_phase_handshake_does_not_wait_for_sdk_notification(self):
        self.initialize(notification=False, request_id='__codex_initialize__')
        owner = self.client.owner(THREAD)
        result = self.client.request('thread/goal/set', {'threadId': THREAD, 'objective': 'one phase fixture', 'status': 'paused', 'origin': 'user'}, owner)
        self.assertEqual(result['goal']['status'], 'paused')
        self.assertEqual(self.read()['method'], 'thread/goal/updated')

    def test_failed_native_initialize_never_enables_goal_writes(self):
        self.send({'id': '__codex_initialize__', 'method': 'initialize', 'params': {'reject': True}})
        self.assertIn('error', self.read())
        with self.assertRaises(GoalUnavailable):
            self.client.owner(THREAD)

    def test_goals_share_original_process_and_injected_responses_do_not_leak(self):
        self.initialize()
        owner = self.client.owner(THREAD)
        result = self.client.request('thread/goal/set', {'threadId': THREAD, 'objective': 'fixture paused goal', 'status': 'paused', 'origin': 'user'}, owner)
        self.assertEqual(result['goal']['status'], 'paused')
        self.assertEqual(self.read()['method'], 'thread/goal/updated')
        self.assertTrue(self.output.empty())
        for status in ['active', 'paused']:
            self.client.request('thread/goal/set', {'threadId': THREAD, 'status': status, 'origin': 'user'}, owner)
            self.assertEqual(self.read()['params']['goal']['status'], status)
            self.assertEqual(self.client.request('thread/goal/get', {'threadId': THREAD}, owner)['goal']['status'], status)
        self.client.request('thread/goal/clear', {'threadId': THREAD, 'origin': 'user'}, owner)
        self.assertIsNone(self.client.request('thread/goal/get', {'threadId': THREAD}, owner)['goal'])
        self.assertTrue(self.output.empty())

    def test_native_notifications_approval_requests_and_responses_pass_unchanged(self):
        self.initialize()
        self.send({'id': 'native-request', 'method': 'native-test', 'params': {'value': 'unchanged'}})
        self.assertEqual(self.read(), {'method': 'native-notification', 'params': {'text': '原样保留'}})
        approval = self.read()
        self.assertEqual(approval['method'], 'item/commandExecution/requestApproval')
        self.assertEqual(self.read(), {'id': 'native-request', 'result': {'value': 'unchanged'}})
        self.send({'id': 'native-approval', 'result': {'decision': 'decline'}})
        self.assertEqual(self.read()['params']['result'], {'decision': 'decline'})

    def test_unloaded_threads_cannot_be_resumed_or_mutated(self):
        self.initialize()
        with self.assertRaises(GoalUnavailable):
            self.client.request('thread/goal/set', {'threadId': OTHER, 'status': 'paused', 'origin': 'user'})
        with self.assertRaises(ValueError):
            self.client.request('thread/resume', {'threadId': OTHER})
        self.assertTrue(self.output.empty())

    def test_local_socket_requires_token_and_user_provenance(self):
        self.initialize()
        owner = self.client.owner(THREAD)
        with self.assertRaises(GoalRPCError):
            self.client._call((owner[0], 'wrong-token'), 'thread/goal/get', {'threadId': THREAD})
        with self.assertRaises(PermissionError):
            self.client.request('thread/goal/set', {'threadId': THREAD, 'status': 'paused'})
        with self.assertRaises(GoalRPCError):
            self.client._call(owner, 'thread/goal/set', {'threadId': THREAD, 'status': 'paused', 'origin': 'automatic'})
        with self.assertRaises(GoalRPCError):
            self.client._call(owner, 'turn/start', {'threadId': THREAD})

    def test_registry_permissions_are_enforced(self):
        self.root.chmod(0o755)
        with self.assertRaises(PermissionError):
            self.client.owner(THREAD)
        self.root.chmod(0o700)


class GoalValidationTests(unittest.TestCase):
    def test_other_chat_goals_and_private_metadata_are_not_exposed(self):
        value = {'threadId': THREAD, 'objective': 'Goal', 'status': 'paused', 'tokensUsed': 0,
                 'timeUsedSeconds': 0, 'createdAt': 10, 'updatedAt': 10, 'privateInstructions': 'not display metadata'}
        self.assertNotIn('privateInstructions', checked_goal(value, THREAD))
        with self.assertRaises(ValueError): checked_goal(value, OTHER)

    def test_revision_excludes_running_counters_but_detects_edits_and_state(self):
        goal = {'threadId': THREAD, 'objective': 'one', 'status': 'active', 'createdAt': 3, 'tokenBudget': None, 'tokensUsed': 1}
        self.assertEqual(goal_revision(goal), goal_revision(dict(goal, tokensUsed=99, timeUsedSeconds=100, updatedAt=20)))
        self.assertNotEqual(goal_revision(goal), goal_revision(dict(goal, objective='two')))
        self.assertNotEqual(goal_revision(goal), goal_revision(dict(goal, status='paused')))
        self.assertNotEqual(goal_revision(goal), goal_revision(None))

    def test_invalid_budget_objective_and_extra_rpc_fields_are_rejected(self):
        for changes in ({'tokenBudget': True}, {'tokenBudget': -1}, {'objective': ''}, {'command': 'bad'}, {'status': 'invented'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_goal_rpc('thread/goal/set', dict(changes, threadId=THREAD, origin='user'))


if __name__ == '__main__': unittest.main()
