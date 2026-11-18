"""Exercise independent accounts, device pairing, and merged relay routing over HTTP."""

import json
from pathlib import Path
import secrets
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from accounts import LEGACY_DEVICE, digest
from server import RelayServer, initialize_config


PROJECT = '10000000-0000-4000-8000-000000000001'
THREAD = '20000000-0000-4000-8000-000000000002'
OPERATION = '30000000-0000-4000-8000-000000000003'
PASSWORD = 'test-private-passphrase'


class MultiAccountTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config, _ = initialize_config(root / 'settings.json', 'http://127.0.0.1', PASSWORD)
        self.server = RelayServer(('127.0.0.1', 0), config, root / 'state', root)
        self.origin = f'http://127.0.0.1:{self.server.server_port}'
        self.server.config['public_origin'] = self.origin
        self.owner = {'Authorization': 'Bearer ' + config['owner_token']}
        self.legacy_agent = {'Authorization': 'Bearer ' + config['agent_token']}
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=2)

    def call(self, path, body=None, headers=None, binary=False):
        payload = json.dumps(body).encode() if isinstance(body, dict) else body
        with urlopen(Request(self.origin + path, data=payload, headers=headers or {}), timeout=5) as response:
            data = response.read()
            return response.headers, data if binary else json.loads(data)

    def expect_status(self, expected, path, body=None, headers=None):
        with self.assertRaises(HTTPError) as raised:
            self.call(path, body, headers)
        self.assertEqual(raised.exception.code, expected)

    def session_headers(self, headers, value):
        return {'Cookie': headers['Set-Cookie'].split(';')[0], 'X-AIRemote-CSRF': value['csrf']}

    def member(self, username):
        invite = self.call('/api/admin/invitations', {'username': username}, self.owner)[1]
        headers, result = self.call('/api/register', {'code': invite['code'], 'password': PASSWORD})
        return self.session_headers(headers, result), invite

    def pairing_body(self, browser, name):
        pair = self.call('/api/account/pairings', {}, browser)[1]
        token = secrets.token_urlsafe(32)
        return {'code': pair['code'], 'tokenHash': digest(token), 'name': name}, {'Authorization': 'Bearer ' + token}

    def pair(self, browser, name):
        body, agent = self.pairing_body(browser, name)
        identifier = self.call('/api/pairing/claim', body)[1]['deviceId']
        store = self.server.device_store(identifier)
        with store.db() as db:
            db.execute('INSERT INTO device VALUES(1,?,?,1,?,?)', (name, time.time(), '', 'test'))
        return identifier, agent, body

    def submit(self, browser, path, key, body=None):
        result = self.call('/api/requests', {'method': 'GET' if body is None else 'POST',
                           'path': path, 'key': key, 'body': body or {}}, browser)[1]
        return '/api/requests/' + result['id']

    def answer(self, agent, observed_path, value, *, status=200):
        job = self.call('/api/agent/poll', {'connectorOnline': True}, agent)[1]['job']
        self.assertEqual(job['request']['path'], observed_path)
        headers = {**agent, 'X-Relay-Lease': job['lease'], 'X-Connector-Status': str(status),
                   'X-Connector-Type': 'application/octet-stream' if isinstance(value, bytes) else 'application/json'}
        self.call('/api/agent/jobs/' + job['id'] + '/result', value if isinstance(value, bytes) else json.dumps(value).encode(), headers)
        return job['request']

    def test_two_macs_merge_by_provider_and_route_same_native_id(self):
        browser, _ = self.member('one-user')
        first_id, first, _ = self.pair(browser, 'First Mac')
        second_id, second, _ = self.pair(browser, 'Second Mac')
        self.assertNotEqual(first_id, second_id)
        result = self.call('/api/session', headers=browser)[1]
        self.assertEqual((result['device']['deviceCount'], result['device']['onlineCount']), (2, 2))
        providers = self.submit(browser, '/api/providers', 'provider-catalog')
        self.answer(first, '/api/providers', {'providers': [{'id': 'codex', 'readable': True, 'connected': True, 'features': ['send']}]})
        self.answer(second, '/api/providers', {'providers': [{'id': 'claude', 'readable': True, 'connected': True, 'features': ['messages']}]})
        self.assertEqual({row['id'] for row in self.call(providers + '/result', headers=browser)[1]['providers']}, {'codex', 'claude'})
        listing = self.submit(browser, '/api/projects', 'project-catalog')
        for name, agent in [('first', first), ('second', second)]:
            path = f'/work/{name}/Shared'
            self.answer(agent, '/api/projects', {'projects': [{'id': PROJECT, 'name': 'Shared', 'path': path, 'roots': [path]}]})
        projects = self.call(listing + '/result', headers=browser)[1]['projects']
        self.assertEqual(len(projects), 1)
        self.assertCountEqual(projects[0]['roots'], ['/work/first/Shared', '/work/second/Shared'])
        group = self.submit(browser, '/api/projects/' + projects[0]['id'] + '/threads', 'group-threads')
        for name, agent in [('first', first), ('second', second)]:
            self.answer(agent, '/api/projects/' + PROJECT + '/threads', {'threads': [{'threadId': THREAD, 'projectId': PROJECT, 'title': name}]})
        threads = self.call(group + '/result', headers=browser)[1]['threads']
        by_name = {row['title']: row['threadId'] for row in threads}
        self.assertNotEqual(by_name['first'], by_name['second'])
        self.assertTrue(all(row['projectId'] == projects[0]['id'] for row in threads))
        request = {'text': 'Only the second Mac should receive this instruction'}
        direct = self.submit(browser, '/api/threads/' + by_name['second'] + '/messages', 'same-request', request)
        self.assertIsNone(self.server.device_store(first_id).claim())
        echoed = self.answer(second, '/api/threads/' + THREAD + '/messages', {'operationId': OPERATION, 'result': {'threadId': THREAD}}, status=202)
        self.assertEqual(echoed['body'], request)
        self.assertEqual(self.submit(browser, '/api/threads/' + by_name['second'] + '/messages', 'same-request', request), direct)
        receipt = self.call(direct + '/result', headers=browser)[1]
        self.assertEqual(receipt['result']['threadId'], by_name['second'])
        poll = self.submit(browser, '/api/operations/' + receipt['operationId'], 'get-receipt')
        self.answer(second, '/api/operations/' + OPERATION, {'requestStatus': 'applied', 'result': {'threadId': THREAD}})
        self.assertEqual(self.call(poll + '/result', headers=browser)[1]['requestStatus'], 'applied')
        artifact = '/artifacts/' + 'a' * 24 + '/download'
        download = self.submit(browser, '/api/threads/' + by_name['first'] + artifact, 'download-file')
        content = b'{"threadId":"must not rewrite a downloaded JSON file"}'
        self.answer(first, '/api/threads/' + THREAD + artifact, content)
        self.assertEqual(self.call(download + '/result', headers=browser, binary=True)[1], content)
        new = self.submit(browser, '/api/threads', 'create-thread', {'cwd': '/work/second/Shared', 'title': 'New', 'text': ''})
        self.assertIsNone(self.server.device_store(first_id).claim())
        self.answer(second, '/api/threads', {'result': {'threadId': THREAD}})
        self.assertEqual(self.call(new + '/result', headers=browser)[1]['result']['threadId'], by_name['second'])

    def test_members_read_only_own_data_and_admin_controls_enrollment(self):
        first, invitation = self.member('first-user')
        second, _ = self.member('second-user')
        self.expect_status(400, '/api/register', {'code': invitation['code'], 'password': PASSWORD})
        self.expect_status(403, '/api/admin', headers=first)
        self.expect_status(401, '/api/account/pairings', {}, {'Cookie': first['Cookie']})
        first_id, first_agent, proof = self.pair(first, 'First Mac')
        second_id, second_agent, _ = self.pair(second, 'Second Mac')
        self.assertEqual(self.call('/api/pairing/claim', proof)[1]['deviceId'], first_id)
        self.expect_status(409, '/api/pairing/claim', {**proof, 'tokenHash': 'b' * 64})
        self.expect_status(404, f'/api/account/devices/{second_id}/revoke', {}, first)
        job = self.submit(first, '/api/projects', 'first-catalog')
        self.answer(first_agent, '/api/projects', {'projects': [{'id': PROJECT, 'name': 'First only', 'roots': ['/private/first']}]})
        self.assertEqual(self.call(job + '/result', headers=first)[1]['projects'][0]['name'], 'First only')
        self.expect_status(404, job + '/result', headers=second)
        self.expect_status(404, job + '/result', headers=self.owner)
        overview = self.call('/api/admin', headers=self.owner)[1]
        self.assertEqual({row['username'] for row in overview['accounts']}, {'admin', 'first-user', 'second-user'})
        self.assertEqual({row['id'] for row in overview['devices']}, {LEGACY_DEVICE, first_id, second_id})
        self.call(f'/api/admin/devices/{second_id}/revoke', {}, self.owner)
        self.expect_status(401, '/api/agent/poll', {'connectorOnline': True}, second_agent)
        first_account = self.call('/api/session', headers=first)[1]['accountId']
        pending = self.submit(first, '/api/projects', 'pause-catalog')
        self.call(f'/api/admin/accounts/{first_account}/enabled', {'enabled': False}, self.owner)
        self.expect_status(401, '/api/session', headers=first)
        self.expect_status(401, '/api/agent/poll', {'connectorOnline': True}, first_agent)
        self.assertIsNone(self.server.device_store(first_id).claim())
        self.expect_status(401, pending, headers=first)

    def test_legacy_cookie_device_and_pending_result_are_preserved(self):
        legacy = secrets.token_urlsafe(32)
        with self.server.store.db() as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(legacy), 'legacy-csrf', time.time() + 100))
            db.execute('INSERT INTO device VALUES(1,?,?,1,?,?)', ('Previous Mac', time.time(), '', '0.9.2'))
        browser = {'Cookie': 'airemote_session=' + legacy, 'X-AIRemote-CSRF': 'legacy-csrf'}
        self.assertEqual(self.call('/api/session', headers=browser)[1]['device']['name'], 'Previous Mac')
        job = self.submit(browser, '/api/threads/' + THREAD, 'legacy-thread')
        self.answer(self.legacy_agent, '/api/threads/' + THREAD, {'threadId': THREAD, 'title': 'Previous conversation'})
        self.assertEqual(self.call(job + '/result', headers=browser)[1]['threadId'], THREAD)
        raw = self.server.store.submit({'method': 'GET', 'path': '/api/providers', 'key': 'before-migration', 'body': {}})
        self.answer(self.legacy_agent, '/api/providers', {'providers': []})
        self.assertEqual(self.call('/api/requests/' + raw['id'] + '/result', headers=browser)[1], {'providers': []})


if __name__ == '__main__':
    unittest.main()
