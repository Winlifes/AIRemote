from pathlib import Path
import plistlib
import subprocess
from tempfile import TemporaryDirectory
import unittest

from mac_background import BackgroundService, LABEL


class BackgroundTests(unittest.TestCase):
    def test_register_once_at_login_and_remove_on_explicit_stop(self):
        with TemporaryDirectory() as directory:
            home = Path(directory)
            commands = []
            loaded = False

            def launchctl(args, **_):
                nonlocal loaded
                commands.append(args[1])
                if args[1] in ('bootstrap', 'bootout'):
                    loaded = args[1] == 'bootstrap'
                return subprocess.CompletedProcess(args, 0 if args[1] != 'print' or loaded else 3)

            service = BackgroundService(home, runner=launchctl)
            config = home / 'private' / 'mac-runtime.json'
            config.parent.mkdir()
            runtime = home / 'AIRemote.app/Contents/Resources/runtime'
            with service.locked():
                service.enable(config, runtime)
                service.enable(config, runtime)
                info = plistlib.loads(service.plist.read_bytes())
                self.assertEqual(info['AssociatedBundleIdentifiers'], ['fun.winlife.airemote'])
                self.assertEqual(info['Label'], LABEL)
                self.assertTrue(info['KeepAlive'] and info['RunAtLoad'])
                self.assertEqual(service.configuration(), config)
                self.assertEqual(service.plist.stat().st_mode & 0o077, 0)
                self.assertEqual(commands.count('bootstrap'), 1)
                with self.assertRaises(RuntimeError):
                    service.enable(home / 'another-config.json', runtime)
                service.disable()
                self.assertFalse(service.plist.exists() or loaded)
                self.assertEqual(commands.count('bootout'), 1)


if __name__ == '__main__':
    unittest.main()
