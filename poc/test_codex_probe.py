import json
from pathlib import Path
import secrets
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_probe import History, ProbeServer, connect_read_only


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.allowed = self.root / "output"
        self.allowed.mkdir()
        self.good = self.allowed / "测试 artifact.txt"
        self.good.write_bytes(b"actual generated artifact\n")
        self.secret = self.root / "private.txt"
        self.secret.write_bytes(b"outside authorized directory")
        (self.allowed / "escape.txt").symlink_to(self.secret)
        sibling = self.root / "output-other"
        sibling.mkdir()
        (sibling / "private.txt").write_text("not authorized")
        self.thread_id = "11111111-1111-4111-8111-111111111111"
        state = sqlite3.connect(self.root / "state_5.sqlite")
        state.execute("CREATE TABLE threads(id TEXT,name TEXT,title TEXT,cwd TEXT,history_mode TEXT,archived INTEGER,updated_at INTEGER)")
        state.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (
            self.thread_id, "test thread", "original prompt", str(self.root), "paginated", 0, 1,
        ))
        state.commit()
        state.close()
        db = sqlite3.connect(self.root / "thread_history_1.sqlite")
        db.execute("CREATE TABLE thread_turns(thread_id TEXT,turn_id TEXT,status TEXT,started_at INTEGER,completed_at INTEGER,final_agent_item_id TEXT,rollout_ordinal INTEGER)")
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)", (self.thread_id, "turn-1", "completed", 1, 2, "item-1", 1))
        db.execute("INSERT INTO thread_turns VALUES(?,?,?,?,?,?,?)", (self.thread_id, "turn-2", "inProgress", 3, None, None, 2))
        db.execute("CREATE TABLE thread_items(thread_id TEXT,turn_id TEXT,item_id TEXT,created_at_ms INTEGER,item_json TEXT,item_type TEXT,rollout_ordinal INTEGER)")
        links = "\n".join("[file](<%s>)" % p for p in [self.good, self.secret, self.allowed / "escape.txt", sibling / "private.txt"])
        item = {"type": "agentMessage", "id": "item-1", "text": links, "phase": "final_answer"}
        db.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", (self.thread_id, "turn-1", "item-1", 2, json.dumps(item), "agentMessage", 1))
        other = {"type": "agentMessage", "id": "other-item", "text": "another thread's private answer", "phase": "final_answer"}
        db.execute("INSERT INTO thread_items VALUES(?,?,?,?,?,?,?)", ("another-thread", "turn-x", "other-item", 2, json.dumps(other), "agentMessage", 2))
        db.commit()
        db.close()
        self.history = History(self.thread_id, [self.allowed], self.root)
        self.token = secrets.token_urlsafe(32)
        self.server = ProbeServer(0, self.history, self.token)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.base = "http://127.0.0.1:%s" % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.worker.join(timeout=3)
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, token=None, method="GET"):
        headers = {"Authorization": "Bearer " + token} if token else {}
        return urlopen(Request(self.base + path, headers=headers, method=method), timeout=3)

    def test_authentication_is_required_for_messages_and_files(self):
        for path in ["/api/session", "/api/messages", "/api/artifacts"]:
            for token in [None, "incorrect-token", "caf\u00e9"]:
                with self.assertRaises(HTTPError) as caught:
                    self.request(path, token)
                self.assertEqual(caught.exception.code, 401)

    def test_status_is_persisted_state_and_does_not_claim_runtime_liveness(self):
        with self.request("/api/session", self.token) as response:
            session = json.load(response)
        self.assertEqual(session["latestPersistedTurnStatus"], "inProgress")
        self.assertEqual(session["runtimeStatus"], "unknown")
        self.assertEqual([t["status"] for t in session["turns"]], ["inProgress", "completed"])

    def test_only_selected_thread_messages_are_returned(self):
        with self.request("/api/messages", self.token) as response:
            messages = json.load(response)["messages"]
        self.assertTrue(messages)
        self.assertEqual({m["threadId"] for m in messages}, {self.thread_id})
        self.assertNotIn("another thread's private answer", str(messages))

    def test_explicit_artifact_links_download_exact_bytes(self):
        with self.request("/api/artifacts", self.token) as response:
            artifacts = json.load(response)["artifacts"]
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["name"], self.good.name)
        with self.request("/api/artifacts/%s/download" % artifacts[0]["id"], self.token) as response:
            self.assertEqual(response.read(), self.good.read_bytes())
            self.assertIn("attachment", response.headers["Content-Disposition"])

    def test_scope_rejects_symlinks_sibling_prefixes_and_arbitrary_paths(self):
        for path in [self.secret, self.allowed / "escape.txt", self.root / "output-other/private.txt"]:
            with self.assertRaises(PermissionError):
                self.history.allowed_file(path)
        for path in ["/api/artifacts/" + "0" * 24 + "/download", "/api/artifacts/../../private.txt/download", "/api/files?path=/etc/passwd"]:
            with self.assertRaises(HTTPError) as caught:
                self.request(path, self.token)
            self.assertEqual(caught.exception.code, 404)

    def test_source_database_cannot_be_written(self):
        db = connect_read_only(self.root / "state_5.sqlite")
        try:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM threads")
        finally:
            db.close()

    def test_http_does_not_claim_to_deliver_new_instructions(self):
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/messages", self.token, method="POST")
        self.assertEqual(caught.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
