import fcntl
import io
import json
from pathlib import Path
import plistlib
import tempfile
import tarfile
import unittest
from unittest.mock import Mock, patch
import zipfile
from urllib.error import URLError

import update_core
import update_mac
from update_core import extract_zip, github_url, release_asset, version_tuple
from update_mac import install_app
from update_server import switch_release


class UpdateSafetyTests(unittest.TestCase):
    def test_transient_download_retries_but_metadata_errors_do_not(self):
        with patch.object(update_core, '_download_once', side_effect=[URLError('temporary CDN failure'), b'correct']), patch.object(update_core.time, 'sleep'):
            self.assertEqual(update_core.download('https://github.com/example'), b'correct')
        with patch.object(update_core, '_download_once', side_effect=ValueError('unsafe URL')) as download:
            with self.assertRaises(ValueError): update_core.download('https://evil.example')
            self.assertEqual(download.call_count, 1)
    def test_only_stable_versions_and_trusted_https_hosts_are_accepted(self):
        self.assertGreater(version_tuple('v0.10.0'), version_tuple('0.9.9'))
        for value in ['v0.6.0-beta', '../0.6.0', 'latest', '1.2']:
            with self.assertRaises(ValueError): version_tuple(value)
        for url in ['http://github.com/a', 'https://github.com.example.org/a', 'file:///tmp/update.zip']:
            with self.assertRaises(ValueError): github_url(url)

    def test_release_cannot_select_an_asset_from_another_repository(self):
        release = {'tag_name': 'v0.6.0', 'assets': [{'name': 'AIRemote-macos.zip',
            'browser_download_url': 'https://github.com/another/repo/releases/download/v0.6.0/AIRemote-macos.zip',
            'digest': 'sha256:' + 'a' * 64}]}
        with self.assertRaises(ValueError): release_asset(release, 'AIRemote-macos.zip')

    def test_checksum_mismatch_leaves_no_installable_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            def bad_download(url, target): Path(target).write_bytes(b'bad'); return 'b' * 64
            with patch.object(update_core, 'download', side_effect=bad_download):
                with self.assertRaises(ValueError): update_core.fetch_asset({'name':'app.zip','url':'ignored','sha256':'a'*64},directory)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_archive_traversal_is_rejected_before_any_file_is_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); archive = root / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('AIRemote.app/good', b'ok'); output.writestr('../escaped', b'bad')
            with self.assertRaises(ValueError): extract_zip(archive, root / 'staged')
            self.assertFalse((root / 'escaped').exists())
            self.assertEqual(list((root / 'staged').iterdir()), [])

    def test_failed_new_runtime_rolls_back_app_and_preserves_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); old = root / 'AIRemote.app'; candidate = root / 'candidate.app'
            for app, text in [(old, 'old'), (candidate, 'new')]:
                (app / 'Contents/Resources/runtime').mkdir(parents=True)
                (app / 'marker').write_text(text)
            config = root / 'runtime.json'; original = b'{"private":"must survive an update"}'
            config.write_bytes(original); calls = []
            def control(path, **options):
                calls.append(options)
                if not options.get('stop') and (old / 'marker').read_text() == 'new': raise RuntimeError('new runtime failed')
            with self.assertRaises(RuntimeError): install_app(candidate, old, config, controller=control)
            self.assertEqual((old / 'marker').read_text(), 'old')
            self.assertEqual(config.read_bytes(), original)
            self.assertTrue(calls[0]['stop']); self.assertEqual(len(calls), 3)
            self.assertTrue(all(call['restart'] for call in calls[1:]))

    def test_automatic_launch_waits_for_mobile_leases_and_local_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan, config, app = root / 'plan.json', root / 'runtime.json', root / 'AIRemote.app'
            plan.touch()
            (app / 'Contents/Resources/runtime').mkdir(parents=True)
            values = {'config': str(config), 'app': str(app), 'installer': 'verified-installer.py'}
            lock = update_mac.automatic_update_lock(config)
            with patch.object(update_mac, 'load_plan', return_value=values), \
                 patch.object(update_mac, 'automatic_update_readiness') as readiness, \
                 patch.object(update_mac, 'detached_installer') as installer:
                with lock.open('a+') as active:
                    fcntl.flock(active, fcntl.LOCK_SH)
                    self.assertEqual(update_mac.launch_install(plan, 1, automatic=True, idle_timeout=0), {'deferred': True})
                readiness.assert_not_called()
                readiness.return_value = (False, True)
                self.assertEqual(update_mac.launch_install(plan, 1, automatic=True), {'deferred': True})
                installer.assert_not_called()

                def launch(path, arguments, descriptors):
                    with lock.open('a+') as active, self.assertRaises(BlockingIOError):
                        fcntl.flock(active, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    self.assertEqual(len(descriptors), 1)
                    self.assertIn('--in-menu-bar', arguments)
                    self.assertIn('--keep-services-stopped', arguments)
                    return {'installerStarted': True}

                readiness.return_value = (True, False)
                installer.side_effect = launch
                self.assertEqual(update_mac.launch_install(plan, 1, automatic=True, in_menu_bar=True), {'installerStarted': True})

    def test_automatic_install_preserves_intentionally_stopped_services(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate, app = root / 'candidate.app', root / 'AIRemote.app'
            candidate.mkdir(); app.mkdir()
            (candidate / 'marker').write_text('new')
            controller = Mock()
            install_app(candidate, app, root / 'runtime.json', controller=controller, restart_services=False)
            controller.assert_not_called()
            self.assertEqual((app / 'marker').read_text(), 'new')

    def test_tar_symlink_and_traversal_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, kind in [('../escape', tarfile.REGTYPE), ('link', tarfile.SYMTYPE)]:
                archive = root / 'bad.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    entry = tarfile.TarInfo(name); entry.type = kind; entry.linkname = '/etc/passwd'
                    output.addfile(entry)
                with self.assertRaises(ValueError): update_core.extract_tar(archive, root / 'staged')
            self.assertFalse((root.parent / 'escape').exists())

    def test_failed_server_health_check_restores_old_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve(); old = root / 'old'; new = root / 'new'
            old.mkdir(); new.mkdir(); current = root / 'current'; current.symlink_to(old)
            calls = []
            with self.assertRaises(RuntimeError):
                switch_release(new, current, '0.6.0', restarter=lambda: calls.append(current.resolve()), health=lambda _: False, timeout=0)
            self.assertEqual(current.resolve(), old)
            self.assertEqual(calls, [new, old])
