"""The Mac keeps the same private device key after an ambiguous pairing response."""

import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import uuid

from pair_mac import pair


class MacPairingTests(unittest.TestCase):
    def test_retry_uses_same_key_and_never_persists_plaintext_pairing_code(self):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            connections = root / 'connections'
            workspace = root / 'workspace'
            workspace.mkdir()
            code = 'local-test-one-time-pairing-code'
            data = {'origin': 'https://test.example', 'code': code,
                    'name': 'Test machine', 'workspace': str(workspace)}
            observed = []
            device_id = str(uuid.uuid4())

            def request(origin, body):
                self.assertEqual(origin, data['origin'])
                self.assertEqual(body['code'], code)
                observed.append(body['tokenHash'])
                if len(observed) == 1:
                    raise TimeoutError('simulated acknowledgement loss')
                return {'deviceId': device_id}

            with self.assertRaises(TimeoutError):
                pair(data, connections, request)
            result = pair(data, connections, request)
            self.assertEqual(result['deviceId'], device_id)
            self.assertEqual(observed[0], observed[1])
            self.assertEqual(len(list(connections.glob('pair-*'))), 1)
            private = Path(result['directory']) / '.local'
            config = json.loads((private / 'mac-runtime.json').read_text())
            agent = json.loads((private / 'relay-agent.json').read_text())
            self.assertEqual(config['workspace_roots'], [str(workspace)])
            self.assertEqual(hashlib.sha256(agent['agent_token'].encode()).hexdigest(), observed[-1])
            for file in private.iterdir():
                self.assertFalse(file.stat().st_mode & 0o077)
                self.assertNotIn(code, file.read_text())
            self.assertNotIn(agent['agent_token'], json.dumps(result))


if __name__ == '__main__':
    unittest.main()
