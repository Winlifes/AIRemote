"""Process-level regressions for double-click startup, using isolated fixtures."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest


LAUNCHER = Path(__file__).with_name('start_mac.py')
CONNECTOR = r'''
import argparse, json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
p=argparse.ArgumentParser(); p.add_argument('--port',type=int); p.add_argument('--state-dir'); args,_=p.parse_known_args()
state=Path(args.state_dir)
(state/'unhealthy').unlink(missing_ok=True)
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        healthy=not (state/'unhealthy').exists()
        self.send_response(200 if healthy else 503); self.end_headers()
        self.wfile.write(json.dumps({'ok':healthy}).encode())
    def log_message(self,*_): pass
server=HTTPServer(('127.0.0.1',args.port),Handler)
try: server.serve_forever(poll_interval=.05)
except KeyboardInterrupt: pass
finally: server.server_close()
'''
AGENT = r'''
import argparse,time
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--config');args=p.parse_args()
if Path(args.config).with_name('agent-fails').exists():raise SystemExit(42)
try:
    while True:time.sleep(1)
except KeyboardInterrupt:pass
'''


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='airemote startup ')
        self.root = Path(self.temp.name)
        for folder in ['poc', 'relay', '.local']:
            (self.root / folder).mkdir()
        (self.root / 'poc/control_server.py').write_text(CONNECTOR)
        (self.root / 'relay/agent.py').write_text(AGENT)
        self.private = self.root / '.local'
        self.config = self.private / 'runtime.json'
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0)); self.port = listener.getsockname()[1]
        token = self.private / 'token'; token.write_text('isolated-startup-test-token'); token.chmod(0o600)
        agent_config = self.private / 'agent.json'; agent_config.write_text('{}'); agent_config.chmod(0o600)
        self.config.write_text(json.dumps({'project':str(self.root), 'threads':['fixture'],
            'artifact_roots':[str(self.root)], 'workspace_roots':[str(self.root)],
            'token_file':str(token), 'connector_state':str(self.private),
            'agent_config':str(agent_config), 'connector_port':self.port,
            'relay_url':'https://example.test'}))
        self.config.chmod(0o600)

    def run_launcher(self, *args):
        return subprocess.run([sys.executable, str(LAUNCHER), '--config', str(self.config), *args],
                              capture_output=True, text=True, timeout=30)

    def start(self):
        result = self.run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return json.loads(result.stdout.splitlines()[-1])

    def tearDown(self):
        try:
            result = self.run_launcher('--stop')
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            self.temp.cleanup()

    def test_repeated_start_reuses_healthy_processes(self):
        first = self.start(); second = self.start()
        self.assertEqual(first['running'], second['running'])
        self.assertEqual(second['started'], [])

    def test_status_is_read_only_and_never_launches_processes(self):
        before=self.run_launcher('--status')
        self.assertEqual(before.returncode,0,before.stderr)
        self.assertEqual(json.loads(before.stdout)['running'],{})
        self.assertFalse((self.private/'mac-processes.json').exists())
        started=self.start()
        after=json.loads(self.run_launcher('--status').stdout)
        self.assertEqual(after['running'],started['running'])
        self.assertTrue(after['connectorHealthy'])
        self.assertTrue(after['agentRunning'])

    def test_live_pid_with_failed_health_is_recovered_without_restarting_agent(self):
        first = self.start()
        (self.private / 'unhealthy').touch()
        second = self.start()
        self.assertNotEqual(first['running']['connector'], second['running']['connector'])
        self.assertEqual(first['running']['agent'], second['running']['agent'])
        self.assertEqual(second['recovered'], ['connector'])

    def test_agent_that_exits_cannot_be_reported_as_success(self):
        (self.private / 'agent-fails').touch()
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('公网连接进程启动后退出', result.stderr)
        self.assertNotIn('terminalMayClose', result.stdout)

    def test_untracked_healthy_connector_is_not_replaced_or_terminated(self):
        first = self.start()
        record = self.private / 'mac-processes.json'
        saved = record.read_text(); record.unlink()
        try:
            result = self.run_launcher()
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('未登记', result.stderr)
            for pid in first['running'].values():
                os.kill(pid, 0)
        finally:
            record.write_text(saved); record.chmod(0o600)


if __name__ == '__main__':
    unittest.main()
