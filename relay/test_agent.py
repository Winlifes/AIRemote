from pathlib import Path
import tempfile
import unittest

from agent import acquire_agent_lock


class AgentInstanceTests(unittest.TestCase):
    def test_only_one_agent_can_consume_for_each_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'agent.json'
            first = acquire_agent_lock(path)
            try:
                with self.assertRaises(RuntimeError): acquire_agent_lock(path)
                other = acquire_agent_lock(Path(directory) / 'other-relay.json')
                other.close()
            finally:
                first.close()
            restarted = acquire_agent_lock(path)
            restarted.close()


if __name__ == '__main__': unittest.main()
