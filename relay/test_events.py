from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from accounts import LEGACY_DEVICE
from agent import Agent
from events import EventFeed
from server import RelayServer, initialize_config


CHAT = '11111111-1111-4111-8111-111111111111'


class EventTests(unittest.TestCase):
    def test_overflow_and_foreign_cursors_force_a_fresh_read(self):
        events = EventFeed(2)
        first, = events.read('account-a', '')
        self.assertEqual(first[1], 'reset')
        events.emit('account-a', 'chat', {'threadId': CHAT})
        events.emit('account-b', 'device')
        replay, = events.read('account-a', first[0], 0)
        self.assertEqual(replay[1:], ('chat', {'threadId': CHAT}))
        foreign, = events.read('account-b', replay[0], 0)
        self.assertEqual(foreign[1:], ('reset', {}))
        events.emit('account-a', 'device')
        events.emit('account-a', 'device')
        recovered, = events.read('account-a', first[0], 0)
        self.assertEqual(recovered[1], 'reset')
        restarted, = EventFeed().read('account-a', recovered[0], 0)
        self.assertEqual(restarted[1], 'reset')

    def test_device_expiry_and_watch_deduplication(self):
        now = [0]
        events = EventFeed(clock=lambda: now[0])
        cursor = events.read('owner', '')[0][0]
        events.heartbeat('owner', 'mac', True)
        events.heartbeat('owner', 'mac', True)
        events.watch('owner', 'mac', '/api/threads/' + CHAT, 'codex', CHAT)
        revisions = {'/api/threads/' + CHAT: 'a' * 64, '/api/threads/other': 'b' * 64}
        events.observe('mac', revisions)
        events.observe('mac', revisions)
        replay = events.read('owner', cursor, 0)
        self.assertEqual([event[1] for event in replay], ['device', 'chat'])
        self.assertEqual(replay[-1][2], {'provider': 'codex', 'threadId': CHAT})
        now[0] += 301
        events.expire()
        self.assertEqual(events.paths('mac'), [])
        self.assertEqual([event[1] for event in events.read('owner', replay[-1][0], 0)], ['device'])

    def test_agent_changes_are_replayed_over_http_after_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / 'connector-token'
            token.write_text(secrets.token_urlsafe(32))
            token.chmod(0o600)
            message = ['private-test-message']

            class Connector(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_GET(self):
                    if self.path == '/api/health':
                        value = {'ok': True}
                    elif self.path == '/api/threads/' + CHAT + '/messages?limit=1':
                        value = {'messages': [{'text': message[0]}]}
                    else:
                        self.send_error(404)
                        return
                    body = json.dumps(value).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            local = ThreadingHTTPServer(('127.0.0.1', 0), Connector)
            config, _ = initialize_config(root / 'relay.json', 'http://127.0.0.1')
            relay = RelayServer(('127.0.0.1', 0), config, root / 'data', root)
            for server in (local, relay):
                threading.Thread(target=server.serve_forever, daemon=True).start()
            server_url = 'http://127.0.0.1:' + str(relay.server_port)
            agent = Agent({'relay_url': server_url, 'agent_token': config['agent_token'],
                           'connector_url': 'http://127.0.0.1:' + str(local.server_port),
                           'connector_token_file': str(token)})
            worker = threading.Thread(target=agent.run, daemon=True)
            worker.start()

            def stream(after=''):
                return urlopen(Request(server_url + '/api/events?thread=' + CHAT + '&cursor=stale',
                                       headers={'Authorization': 'Bearer ' + config['owner_token'], 'Last-Event-ID': after}), timeout=10)

            def next_chat(response, resumed=False):
                event = {}
                while line := response.readline().decode():
                    if line == '\n':
                        if resumed:
                            self.assertNotEqual(event.get('event'), 'reset')
                        if event.get('event') == 'chat':
                            return event
                        event = {}
                    elif ': ' in line:
                        key, value = line.strip().split(': ', 1)
                        event[key] = value
                self.fail('Expected a chat event before the stream closed')

            try:
                with stream() as initial:
                    first = next_chat(initial)
                message[0] = 'second-private-test-message'
                replay = relay.events.read('owner', first['id'], timeout=8)
                self.assertTrue(replay and replay[0][1] == 'chat')
                with stream(first['id']) as continued:
                    second = next_chat(continued, resumed=True)
                self.assertNotEqual(first['id'], second['id'])
                self.assertEqual(json.loads(second['data']), {'provider': 'codex', 'threadId': CHAT})
                self.assertNotIn('private-test-message', json.dumps(second))
            finally:
                agent.stop.set()
                relay.accounts.revoke_device(LEGACY_DEVICE)
                relay.device_store(LEGACY_DEVICE).disconnect()
                worker.join(timeout=4)
                relay.shutdown()
                relay.server_close()
                local.shutdown()
                local.server_close()
