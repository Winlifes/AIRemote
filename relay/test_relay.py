import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from protocol import validate_request
from server import Conflict, initialize_config, RelayServer, Store

THREAD = '11111111-1111-4111-8111-111111111111'


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config, _ = initialize_config(self.root / 'config.json', 'http://127.0.0.1', 'Local-QA-Only!')
        self.server = RelayServer(('127.0.0.1', 0), self.config, self.root / 'state', self.root)
        self.config['public_origin'] = 'http://127.0.0.1:' + str(self.server.server_port)
        self.url = self.config['public_origin']
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True); self.worker.start()
        self.request = {'method': 'POST', 'path': '/api/threads/' + THREAD + '/messages', 'body': {'text': 'hello'}, 'key': 'test-request-001'}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.worker.join(); self.temp.cleanup()

    def call(self, path, body=None, headers=None, role=None, raw=False):
        headers = dict(headers or {})
        if role:
            headers['Authorization'] = 'Bearer ' + self.config[role + '_token']
        data = json.dumps(body).encode() if body is not None else None
        with urlopen(Request(self.url + path, data=data, headers=headers), timeout=3) as response:
            content = response.read()
            return response.status, dict(response.headers), content if raw else json.loads(content)

    def online(self):
        with self.server.store.db() as db:
            db.execute("INSERT INTO device VALUES(1,'test Mac',?,1,'','test')", (time.time(),))

    def test_unauthenticated_is_denied_and_no_history_leaks(self):
        with self.assertRaises(HTTPError) as error:
            self.call('/api/device')
        self.assertEqual(error.exception.code, 401)

    def test_agent_cannot_act_as_browser_owner(self):
        with self.assertRaises(HTTPError) as error:
            self.call('/api/requests', self.request, role='agent')
        self.assertEqual(error.exception.code, 401)

    def test_cookie_csrf_and_cross_origin(self):
        _, headers, session = self.call('/api/login', {'username': 'admin', 'password': 'Local-QA-Only!'})
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        with self.assertRaises(HTTPError):
            self.call('/api/requests', self.request, {'Cookie': cookie})
        with self.assertRaises(HTTPError):
            self.call('/api/requests', self.request, {'Cookie': cookie, 'X-AIRemote-CSRF': session['csrf'], 'Origin': 'https://evil.example'})
        self.online()
        status, _, job = self.call('/api/requests', self.request, {'Cookie': cookie, 'X-AIRemote-CSRF': session['csrf']})
        self.assertEqual(status, 202)
        self.call('/api/logout', {}, {'Cookie': cookie, 'X-AIRemote-CSRF': session['csrf']})
        with self.assertRaises(HTTPError):
            self.call('/api/session', headers={'Cookie': cookie})

    def test_offline_does_not_queue_instructions(self):
        with self.assertRaises(HTTPError) as error:
            self.call('/api/requests', self.request, role='owner')
        self.assertEqual(error.exception.code, 503)
        with self.server.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_duplicate_submit_and_different_payload(self):
        self.online()
        first = self.call('/api/requests', self.request, role='owner')[2]
        again = self.call('/api/requests', self.request, role='owner')[2]
        self.assertEqual(first['id'], again['id'])
        self.request['body']['text'] = 'changed'
        with self.assertRaises(HTTPError) as error:
            self.call('/api/requests', self.request, role='owner')
        self.assertEqual(error.exception.code, 409)

    def test_atomic_claim_only_one_agent_and_response_integrity(self):
        self.online()
        job = self.server.store.submit(self.request)
        claimed = []
        workers = [threading.Thread(target=lambda: claimed.append(self.server.store.claim())) for _ in range(8)]
        for worker in workers: worker.start()
        for worker in workers: worker.join()
        live = [x for x in claimed if x]
        self.assertEqual(len(live), 1)
        content = b'\x00AIRemote\xfffile\x00'
        with self.assertRaises(Conflict):
            self.server.store.finish(job['id'], 'wrong-lease', 200, {}, content)
        self.server.store.finish(job['id'], live[0]['lease'], 200, {'Content-Type': 'application/zip'}, content)
        _, _, body = self.call('/api/requests/' + job['id'] + '/result', role='owner', raw=True)
        self.assertEqual(hashlib.sha256(body).hexdigest(), hashlib.sha256(content).hexdigest())

    def test_expired_claim_is_unknown_not_completed(self):
        self.online()
        job = self.server.store.submit(self.request)
        self.server.store.claim()
        with self.server.store.db() as db:
            db.execute('UPDATE jobs SET expires=?', (time.time() - 1,))
        self.assertEqual(self.server.store.get(job['id'])['state'], 'unknown')

    def test_lease_retry_preserves_request_key(self):
        self.online()
        self.server.store.submit(self.request)
        first = self.server.store.claim()
        with self.server.store.db() as db:
            db.execute('UPDATE jobs SET lease_until=?', (time.time() - 1,))
        second = self.server.store.claim()
        self.assertEqual(first['request']['key'], second['request']['key'])
        self.assertNotEqual(first['lease'], second['lease'])
        with self.assertRaises(Conflict):
            self.server.store.finish(first['id'], first['lease'], 200, {}, b'old')

    def test_url_allowlist_blocks_ssrf_and_traversal(self):
        for path in ['http://127.0.0.1:22', '//evil/api/threads', '/api/../../etc/passwd', '/api/threads%2Fother', '/api/threads?url=http://evil', '/api/threads\\evil']:
            with self.assertRaises(ValueError):
                validate_request({**self.request, 'path': path})
        validate_request({**self.request, 'method': 'GET', 'path': '/api/threads/' + THREAD + '/messages?limit=1000'})

    def test_result_ttl_removes_file_and_request_text(self):
        self.online()
        job = self.server.store.submit(self.request); lease = self.server.store.claim()['lease']
        self.server.store.finish(job['id'], lease, 200, {}, b'secret response')
        with self.server.store.db() as db:
            db.execute('UPDATE jobs SET finished=?', (time.time() - 901,))
        self.server.store.cleanup()
        self.assertFalse((self.server.store.blobs / job['id']).exists())
        self.assertEqual(self.server.store.get(job['id'])['request'], '{}')
        self.assertEqual(self.server.store.get(job['id'])['state'], 'result_expired')
        # An already processed response from an older release must never be
        # displayed as a command that was not executed.
        with self.server.store.db() as db:
            db.execute("UPDATE jobs SET state='expired' WHERE id=?", (job['id'],))
        reopened = Store(self.server.store.root)
        self.assertEqual(reopened.get(job['id'])['state'], 'result_expired')


if __name__ == '__main__':
    unittest.main()
