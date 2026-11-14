import hashlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError
import update_core


class Response(io.BytesIO):
    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {} if length is None else {'Content-Length': str(length)}
    def read(self, size=-1):
        return super().read(min(size, 2048))


class ProgressTests(unittest.TestCase):
    def transfer(self, length, expected=None):
        body = bytes(range(256)) * 40
        events = []
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'update.partial'
            response = Response(body, length)
            class Opener:
                def open(self, *args, **kwargs): return response
            with patch.object(update_core, 'build_opener', return_value=Opener()), patch.object(update_core.time, 'monotonic', side_effect=(n / 5 for n in range(100))):
                digest = update_core.download('https://github.com/Winlifes/AIRemote', target, progress=events.append, expected_size=expected)
            self.assertEqual(digest, hashlib.sha256(body).hexdigest())
            self.assertEqual(target.read_bytes(), body)
        return events, len(body)

    def test_known_length_has_intermediate_monotonic_byte_counts(self):
        events, size = self.transfer(10240)
        values = [item for item in events if item['stage'] == 'downloading']
        self.assertEqual(values[0]['downloadedBytes'], 0)
        self.assertEqual(values[-1]['downloadedBytes'], size)
        self.assertTrue(any(0 < item['downloadedBytes'] < size for item in values))
        self.assertEqual([item['downloadedBytes'] for item in values], sorted(item['downloadedBytes'] for item in values))
        self.assertTrue(all(item['totalBytes'] == size for item in values))

    def test_unknown_length_is_indeterminate_and_asset_size_is_a_fallback(self):
        events, size = self.transfer(None)
        self.assertIsNone(events[-1]['totalBytes'])
        events, _ = self.transfer(None, size)
        self.assertEqual(events[-1]['totalBytes'], size)

    def test_retry_reports_new_attempt(self):
        events = []
        with patch.object(update_core, '_download_once', side_effect=[URLError('network'), b'ok']), patch.object(update_core.time, 'sleep'):
            self.assertEqual(update_core.download('https://github.com/example', progress=events.append), b'ok')
        self.assertEqual(events, [{'event': 'update-progress', 'stage': 'retrying', 'attempt': 2}])

    def test_failed_checksum_never_signals_prepared(self):
        events = []
        with tempfile.TemporaryDirectory() as directory:
            def transfer(url, target, **kwargs):
                Path(target).write_bytes(b'bad')
                return '0' * 64
            with patch.object(update_core, 'download', side_effect=transfer), self.assertRaises(ValueError):
                update_core.fetch_asset({'name': 'app.zip', 'url': 'https://github.com/example', 'sha256': '1' * 64}, directory, progress=events.append)
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual([item['stage'] for item in events], ['verifying'])
