"""Run the Windows connector and outbound agent against isolated native records."""

import json
import os
from pathlib import Path
import secrets
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
for directory in ('relay', 'poc', 'deploy'):
    sys.path.insert(0, str(ROOT / directory))

from accounts import digest
from agent import Agent
from connector import Connector, CodexWindows
from local_lock import exclusive_lock
from pair_mac import pair
from server import RelayServer, initialize_config


THREAD = '20000000-0000-4000-8000-000000000002'


class WindowsConnectorTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'nt', 'Windows ACLs require Windows')
    def test_launcher_prepares_private_user_configuration_directory(self):
        from app import private_directory
        with TemporaryDirectory() as directory, patch.dict(os.environ, {'LOCALAPPDATA': directory}):
            self.assertEqual(private_directory(), Path(directory).resolve() / 'AIRemote')

    def test_lock_releases_on_close(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'session.lock'
            with exclusive_lock(path):
                with self.assertRaises(OSError):
                    exclusive_lock(path)
            exclusive_lock(path).close()

    def test_paired_windows_provider_history_and_files_over_relay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            work = root / 'Fixture Workspace'
            work.mkdir()
            report = work / 'report.txt'
            report.write_bytes(b'Windows report')
            codex = root / 'codex-home'
            sessions = codex / 'sessions' / '2026'
            sessions.mkdir(parents=True)
            records = [
                {'type': 'session_meta', 'payload': {'id': THREAD, 'cwd': str(work)}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn'}},
                {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': 'Generate report'}},
                {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': f'[Report]({report.as_uri()})'}},
                {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn'}},
            ]
            (sessions / f'rollout-fixture-{THREAD}.jsonl').write_text(''.join(json.dumps(record) + '\n' for record in records), encoding='utf-8')
            child = '20000000-0000-4000-8000-000000000003'
            child_meta = {'type': 'session_meta', 'payload': {'id': child, 'cwd': str(work),
                'source': {'subagent': {'thread_spawn': {'parent_thread_id': THREAD}}}}}
            (sessions / f'rollout-fixture-{child}.jsonl').write_text(json.dumps(child_meta) + '\n', encoding='utf-8')
            config, _ = initialize_config(root / 'server.json', 'http://127.0.0.1')
            relay = RelayServer(('127.0.0.1', 0), config, root / 'relay', root)
            relay.daemon_threads = False
            site = f'http://127.0.0.1:{relay.server_port}'
            config['public_origin'] = site
            owner = {'Authorization': 'Bearer ' + config['owner_token']}

            def call(endpoint, payload=None, headers=owner, binary=False):
                with urlopen(Request(site + endpoint, data=json.dumps(payload).encode() if payload is not None else None, headers=headers), timeout=5) as response:
                    return response.read() if binary else json.load(response)

            def request(endpoint, binary=False):
                rid = call('/api/requests', {'method': 'GET', 'path': endpoint, 'key': secrets.token_urlsafe(18)})['id']
                path = '/api/requests/' + rid
                for _ in range(100):
                    if call(path)['state'] == 'done':
                        return call(path + '/result', binary=binary)
                    time.sleep(.05)
                self.fail('Windows agent did not return its result')

            relay_thread = threading.Thread(target=relay.serve_forever, daemon=True)
            relay_thread.start()
            connector = agent = agent_thread = None
            try:
                pair_code = call('/api/account/pairings', {})['code']
                result = pair({'origin': 'https://test.example', 'code': pair_code, 'name': 'Test Windows', 'workspace': str(work)},
                              root / 'connections', lambda _, body: call('/api/pairing/claim', {**body, 'platform': 'windows'}, {}))
                private = Path(result['directory']) / '.local'
                token = (private / 'access-token').read_text().strip()
                connector = Connector(0, token, CodexWindows([work], codex))
                threading.Thread(target=connector.serve_forever, daemon=True).start()
                agent_config = json.loads((private / 'relay-agent.json').read_text())
                agent = Agent({**agent_config, 'relay_url': site, 'connector_url': f'http://127.0.0.1:{connector.server_port}'})
                agent_thread = threading.Thread(target=agent.run, daemon=True)
                agent_thread.start()
                for _ in range(100):
                    if call('/api/device')['connectorOnline']:
                        break
                    time.sleep(.05)
                with relay.device_store(result['deviceId']).db() as db:
                    self.assertIsNotNone(db.execute('SELECT 1 FROM device WHERE connector_online=1').fetchone())
                providers = request('/api/providers')['providers']
                self.assertEqual(providers[0]['clientName'], 'Codex Windows')
                self.assertNotIn('send', providers[0]['features'])
                project, = request('/api/projects')['projects']
                self.assertEqual(project['roots'], [str(work)])
                chat, = request('/api/projects/' + project['id'] + '/threads')['threads']
                self.assertFalse(chat['writeAvailable'])
                path = '/api/threads/' + chat['threadId']
                self.assertEqual(request(path)['latestPersistedTurnStatus'], 'completed')
                messages = request(path + '/messages?limit=20')['messages']
                self.assertEqual([message['role'] for message in messages], ['user', 'assistant'])
                artifact, = request(path + '/artifacts')['artifacts']
                self.assertEqual(request(path + '/artifacts/' + artifact['id'] + '/download', binary=True), b'Windows report')
                device = next(item for item in call('/api/account')['devices'] if item['id'] == result['deviceId'])
                self.assertEqual(device['platform'], 'windows')
                with self.assertRaises(HTTPError) as failure:
                    call('/not-found.py')
                self.assertEqual(failure.exception.code, 404)
            finally:
                if agent:
                    agent.stop.set()
                    relay.accounts.revoke_device(result['deviceId'])
                    relay.device_store(result['deviceId']).disconnect()
                if agent_thread:
                    agent_thread.join(timeout=4)
                if connector:
                    connector.shutdown(); connector.server_close()
                relay.shutdown(); relay.server_close(); relay_thread.join(timeout=4)


if __name__ == '__main__':
    unittest.main()
