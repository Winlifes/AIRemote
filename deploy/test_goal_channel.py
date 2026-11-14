import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import setup_goal_channel as channel


class GoalLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ag-', dir='/tmp')
        self.root = Path(self.temp.name); self.root.chmod(0o700)
        self.bundle = self.root / 'Codex.app'
        self.executable = self.bundle / 'Contents/Resources/codex'
        self.executable.parent.mkdir(parents=True)
        self.executable.write_text('#!/bin/sh\nprintf "fixture-version\\n"\n'); self.executable.chmod(0o700)
        self.info = {'CFBundleIdentifier': 'com.openai.codex', 'CFBundleShortVersionString': '26.1111.21308', 'CFBundleExecutable': 'Codex'}
        self.directory = self.root / 'private'
        self.locate = patch.object(channel, 'locate_codex', return_value=(self.bundle, self.info, self.executable))
        self.locate.start()

    def tearDown(self):
        self.locate.stop(); self.temp.cleanup()

    def test_preparation_is_private_idempotent_and_does_not_launch_codex(self):
        with patch.object(channel.subprocess, 'run') as run:
            first = channel.prepare(self.directory)
            token = json.loads((self.directory / 'config.json').read_text())['token']
            second = channel.prepare(self.directory)
            self.assertEqual(first, second)
            self.assertEqual(token, json.loads((self.directory / 'config.json').read_text())['token'])
            run.assert_not_called()
        for path in self.directory.iterdir(): self.assertEqual(path.stat().st_mode & 0o077, 0)
        result = subprocess.run([first['launcher'], '--version'], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'fixture-version')
        self.assertEqual(list(self.directory.glob('s*.sock')), [])

    def test_running_codex_is_never_quit_or_relaunched(self):
        native = str(self.bundle / 'Contents/MacOS/Codex')
        with patch.object(channel.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, native + '\n', '')) as run:
            result = channel.launch(self.directory)
        self.assertTrue(result['needsRestart']); self.assertFalse(result['launched'])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][0], '/bin/ps')

    def test_launch_uses_per_process_cli_override_and_no_global_environment_change(self):
        with patch.object(channel.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as run:
            result = channel.launch(self.directory)
        self.assertTrue(result['launched'])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ['/usr/bin/open', '--env', 'CODEX_CLI_PATH=' + str(self.directory.resolve() / 'codex'), '-a', str(self.bundle)])


if __name__ == '__main__': unittest.main()
