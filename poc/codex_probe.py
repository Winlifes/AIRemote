#!/usr/bin/env python3
"""Read an existing Codex desktop thread without starting another agent.

This proof of concept targets the paginated history schema observed in the
installed macOS app. SQLite is opened read-only. The HTTP server is loopback
only, has one explicitly selected thread, and never accepts arbitrary paths.
"""

import argparse
import base64
from contextlib import closing
import hashlib
import hmac
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import islice
from urllib.parse import quote, unquote, urlsplit

from codex_rollout import RolloutTail, history_base, inherited_history, managed_rollout, rollout_reader, rollout_roots


MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
LOCAL_LINK = re.compile(r"\]\((?:<([^>]+)>|([^\n)]+))\)")


def connect_read_only(path):
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.execute("PRAGMA query_only=ON")
    connection.row_factory = sqlite3.Row
    return connection


def find_database(home, prefix):
    candidates = list(home.glob(prefix + "_*.sqlite"))
    candidates.sort(key=lambda p: int(p.stem.rsplit("_", 1)[1]), reverse=True)
    if not candidates:
        raise FileNotFoundError("Missing Codex database: " + prefix)
    return candidates[0]


class History:
    def __init__(self, thread_id, artifact_roots, codex_home=None):
        self.thread_id = str(uuid.UUID(thread_id))
        self.home = Path(codex_home or Path.home() / ".codex")
        self.artifact_roots = tuple(Path(p).resolve(strict=True) for p in artifact_roots)
        if not self.artifact_roots or any(not p.is_dir() for p in self.artifact_roots):
            raise ValueError("At least one existing artifact directory is required")
        self.state_path = find_database(self.home, "state")
        self.history_path = find_database(self.home, "thread_history")

    def snapshot(self):
        db = connect_read_only(self.state_path)
        try:
            row = db.execute(
                "SELECT id, name, title, cwd, history_mode, archived, updated_at "
                "FROM threads WHERE id=?", (self.thread_id,),
            ).fetchone()
        finally:
            db.close()
        if row is None:
            raise LookupError("Thread does not exist in this host's database")
        thread = dict(row)
        if thread["history_mode"] != "paginated":
            raise ValueError("This PoC supports paginated history only")
        with closing(connect_read_only(self.history_path)) as db:
            db.execute('BEGIN')
            tail = self._rollout_tail(db)
            turns = self._turns(db, tail, limit=20)
        return {
            "threadId": thread["id"],
            "title": thread["name"] or thread["title"],
            "cwd": thread["cwd"],
            "archived": bool(thread["archived"]),
            "historyMode": thread["history_mode"],
            "source": "local history and rollout, opened read-only" if tail else "local SQLite, opened read-only",
            "latestPersistedTurnStatus": turns[0]["status"] if turns else None,
            "runtimeStatus": "unknown",
            "runtimeStatusNote": "Persisted turn status does not prove runtime liveness or approval state.",
            "turns": turns,
        }

    def messages(self, limit=200):
        return self.message_page(limit=limit, include_turns=False)['messages']

    def message_page(self, limit=40, before=None, include_turns=True):
        limit = max(1, min(int(limit), 1000))
        bound = None
        if before:
            try:
                decoded = json.loads(base64.urlsafe_b64decode(before + '=' * (-len(before) % 4)))
                if (decoded.get('v') != 1 or decoded.get('t') != self.thread_id or
                        not isinstance(decoded.get('o'), int) or decoded['o'] < 0 or
                        not isinstance(decoded.get('i'), str) or len(decoded['i']) > 256):
                    raise ValueError('Invalid history cursor')
                bound = decoded
            except (ValueError, TypeError, KeyError, AttributeError) as error:
                raise ValueError('Invalid history cursor for this chat') from error
        with closing(connect_read_only(self.history_path)) as db:
            db.execute('BEGIN')
            tail = self._rollout_tail(db)
            where = "item_type IN ('userMessage','agentMessage')"
            params = []
            if bound:
                where += ' AND (rollout_ordinal<? OR (rollout_ordinal=? AND item_id<?))'
                params.extend([bound['o'], bound['o'], bound['i']])
            recent = list(islice((m for m in reversed(tail.messages) if not bound or
                                 (m['ordinal'], m['itemId']) < (bound['o'], bound['i'])), limit + 1)) if tail else []
            recent.extend(self._projected_messages(db, tail, where, params, limit + 1 - len(recent)))
            has_more = len(recent) > limit
            messages = list(reversed(recent[:limit]))
            turns = self._turns(db, tail, {m['turnId'] for m in messages}) if include_turns else []
        cursor = None
        if has_more:
            oldest = messages[0]
            cursor = base64.urlsafe_b64encode(json.dumps({'v': 1, 't': self.thread_id, 'o': oldest['ordinal'],
                'i': oldest['itemId']}, separators=(',', ':')).encode()).decode().rstrip('=')
        return {'messages': messages, 'hasMore': has_more, 'olderCursor': cursor, 'turns': turns, 'pageSize': limit}

    def _rollout_tail(self, db):
        try:
            with closing(connect_read_only(self.state_path)) as state:
                def lookup(thread_id):
                    row = state.execute('SELECT rollout_path FROM threads WHERE id=?', (thread_id,)).fetchone()
                    return row['rollout_path'] if row else None

                roots = rollout_roots(self.home)
                path = managed_rollout(lookup(self.thread_id), roots)
                if path is None:
                    return None
                checkpoint = None
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='thread_history_projection_state'").fetchone():
                    checkpoint = db.execute('SELECT next_rollout_byte_offset,next_rollout_ordinal FROM thread_history_projection_state '
                                            'WHERE thread_id=?', (self.thread_id,)).fetchone()
                current = rollout_reader(path, self.thread_id).tail(checkpoint)
                if current is None:
                    return None
                metadata, tail = current
                base = history_base(metadata)
                if base and (base.rollout_id != self.thread_id or checkpoint is None or checkpoint[1] < base.ordinal):
                    return self._merge_inherited(roots, lookup, path, metadata, base, tail)
                return tail
        except sqlite3.OperationalError as e:
            if 'no such column: rollout_path' not in str(e):
                raise
        except (OSError, ValueError, TypeError, RuntimeError, EOFError):
            pass
        return None

    def _merge_inherited(self, roots, lookup, path, metadata, base, tail):
        if metadata.get('attached_thread') or metadata.get('subagent_history_start_ordinal') is not None:
            return tail
        try:
            messages, turns = inherited_history(roots, lookup, path, self.thread_id, base)
        except (OSError, ValueError, EOFError):
            # The visible leaf remains readable when an old archive is absent.
            return tail
        messages.update((message['itemId'], message) for message in tail.messages)
        for turn_id, turn in tail.turns.items():
            turns[turn_id] = {**turns.get(turn_id, {}), **turn}
        ordered = tuple(sorted(messages.values(), key=lambda item: (item['ordinal'], item['itemId'])))
        return RolloutTail(0, ordered, turns)

    def _prefix(self, tail):
        if tail is None:
            return 'thread_id=?', [self.thread_id]
        return 'thread_id=? AND rollout_ordinal<?', [self.thread_id, tail.cutoff]

    def _projected_messages(self, db, tail, where, params, limit):
        if not limit:
            return []
        prefix, keys = self._prefix(tail)
        rows = db.execute('SELECT turn_id,item_id,created_at_ms,item_json,rollout_ordinal FROM thread_items WHERE '
                          + prefix + ' AND ' + where + ' ORDER BY rollout_ordinal DESC,item_id DESC LIMIT ?',
                          keys + params + [limit])
        return [self._format_item(row) for row in rows]

    def _turns(self, db, tail, turn_ids=None, limit=None):
        fields = 'turn_id,status,started_at,completed_at,final_agent_item_id,rollout_ordinal'
        prefix, keys = self._prefix(tail)
        sql = 'SELECT ' + fields + ' FROM thread_turns WHERE ' + prefix
        turns = {}
        if turn_ids is None:
            for row in db.execute(sql + ' ORDER BY rollout_ordinal DESC LIMIT ?', keys + [limit]):
                turns[row['turn_id']] = dict(row)
        else:
            ids = list(turn_ids)
            for offset in range(0, len(ids), 200):
                batch = ids[offset:offset + 200]
                for row in db.execute(sql + ' AND turn_id IN (' + ','.join('?' for _ in batch) + ')', keys + batch):
                    turns[row['turn_id']] = dict(row)
        if tail:
            for turn_id, turn in tail.turns.items():
                if turn_ids is None or turn_id in turn_ids:
                    existing = turns.get(turn_id, {})
                    turns[turn_id] = {**existing, **turn, 'rollout_ordinal': existing.get('rollout_ordinal', turn['rollout_ordinal'])}
        ordered = sorted(turns.values(), key=lambda t: (t['rollout_ordinal'], t['turn_id']), reverse=True)[:limit]
        return [{key: turn.get(key) for key in fields.split(',') if key != 'rollout_ordinal'} for turn in ordered]

    def _format_item(self, row):
        item = json.loads(row['item_json'])
        assistant = item['type'] == 'agentMessage'
        text = item.get('text', '') if assistant else '\n'.join(part.get('text', '') for part in item.get('content', []) if part.get('type') == 'text')
        return {'threadId': self.thread_id, 'turnId': row['turn_id'], 'itemId': row['item_id'],
            'createdAtMs': row['created_at_ms'], 'ordinal': row['rollout_ordinal'], 'clientId': item.get('clientId'),
            'role': 'assistant' if assistant else 'user', 'phase': item.get('phase'), 'text': text}

    def delivery_receipt(self, client_id):
        with closing(connect_read_only(self.history_path)) as db:
            db.execute('BEGIN')
            tail = self._rollout_tail(db)
            user = next((message for message in reversed(tail.messages)
                         if message['role'] == 'user' and message['clientId'] == client_id), None) if tail else None
            if user is None:
                matches = self._projected_messages(db, tail, "item_type='userMessage' AND json_extract(item_json,'$.clientId')=?",
                                                   [client_id], 1)
                user = matches[0] if matches else None
            if user is None:
                return {'receivedInHistory': False}
            turn_id = user['turnId']
            turns = self._turns(db, tail, {turn_id})
            later = list(islice((m for m in reversed(tail.messages) if m['turnId'] == turn_id and m['role'] == 'assistant'
                                 and m['ordinal'] >= user['ordinal']), 8)) if tail else []
            later.extend(self._projected_messages(db, tail, "turn_id=? AND item_type='agentMessage' AND created_at_ms>=?",
                                                 [turn_id, user['createdAtMs']], 8 - len(later)))
            return {'receivedInHistory': True, 'turnId': user['turnId'], 'userItemId': user['itemId'],
                'turnStatus': turns[0]['status'] if turns else None,
                'laterAssistantMessages': list(reversed(later)), 'objectiveCompletionInferred': False}

    def allowed_file(self, path):
        resolved = Path(path).resolve(strict=True)
        if not any(resolved.is_relative_to(root) for root in self.artifact_roots):
            raise PermissionError("File is outside the authorized artifact directories")
        if not resolved.is_file():
            raise PermissionError("Artifact is not a regular file")
        return resolved

    def artifacts(self):
        result = {}
        for message in self.messages():
            if message["role"] != "assistant":
                continue
            for match in LOCAL_LINK.finditer(message["text"]):
                raw = unquote((match.group(1) or match.group(2)).strip())
                if not raw.startswith("/") or raw.startswith("//"):
                    continue
                try:
                    path = self.allowed_file(raw)
                except (OSError, PermissionError, ValueError):
                    continue
                artifact_id = hashlib.sha256(str(path).encode()).hexdigest()[:24]
                result[artifact_id] = {
                    "id": artifact_id, "name": path.name, "path": str(path),
                    "size": path.stat().st_size, "threadId": self.thread_id,
                    "turnId": message["turnId"], "itemId": message["itemId"],
                    "phase": message["phase"],
                    "association": "explicit local link in an assistant message",
                }
        return list(result.values())

    def read_artifact(self, artifact_id):
        artifact = next((a for a in self.artifacts() if a["id"] == artifact_id), None)
        if artifact is None:
            raise LookupError("Unknown artifact for this thread")
        path = self.allowed_file(artifact["path"])
        if path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise ValueError("Artifact exceeds this PoC's 64 MiB limit")
        # Recheck scope immediately before opening. The PoC is for a trusted
        # single-user host; production should use immutable artifact snapshots.
        with path.open("rb") as source:
            content = source.read(MAX_DOWNLOAD_BYTES + 1)
        if len(content) > MAX_DOWNLOAD_BYTES:
            raise ValueError("Artifact exceeds this PoC's 64 MiB limit")
        return artifact, content


class ProbeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port, history, token):
        if len(token) < 24:
            raise ValueError("Use a random bearer token of at least 24 characters")
        self.history = history
        self.token = token
        super().__init__(("127.0.0.1", port), ProbeHandler)


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "AIRemotePoC/0.1"

    def log_message(self, *_):
        pass

    def respond(self, status, content, content_type="application/json; charset=utf-8", extra=None):
        if not isinstance(content, bytes):
            content = json.dumps(content, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        expected = "Bearer " + self.server.token
        actual = self.headers.get("Authorization", "").encode("utf-8")
        if not hmac.compare_digest(actual, expected.encode("utf-8")):
            self.respond(401, {"error": "Authentication required"})
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/session":
                self.respond(200, self.server.history.snapshot())
            elif path == "/api/messages":
                self.respond(200, {"messages": self.server.history.messages()})
            elif path == "/api/artifacts":
                self.respond(200, {"artifacts": self.server.history.artifacts()})
            elif re.fullmatch(r"/api/artifacts/[0-9a-f]{24}/download", path):
                artifact, content = self.server.history.read_artifact(path.split("/")[3])
                mime = mimetypes.guess_type(artifact["name"])[0] or "application/octet-stream"
                self.respond(200, content, mime, {
                    "Content-Disposition": "attachment; filename*=UTF-8''" + quote(artifact["name"], safe=""),
                    "ETag": '"' + hashlib.sha256(content).hexdigest() + '"',
                })
            else:
                self.respond(404, {"error": "Unknown endpoint"})
        except (LookupError, FileNotFoundError):
            self.respond(404, {"error": "Thread or artifact not found"})
        except PermissionError:
            self.respond(403, {"error": "Artifact is outside the authorized directory"})
        except ValueError as error:
            self.respond(400, {"error": str(error)})
        except (sqlite3.Error, OSError):
            self.respond(503, {"error": "Local Codex history is temporarily unavailable"})

    def do_POST(self):
        self.respond(405, {
            "error": "HTTP instruction delivery is intentionally unavailable: an external live write transport has not passed validation."
        })


def enqueue(thread_id, message_file):
    """An official queue receipt is deliberately not called a delivery receipt."""
    thread_id = str(uuid.UUID(thread_id))
    message = Path(message_file).read_text(encoding="utf-8")
    if not message.strip():
        raise ValueError("The message must not be empty")
    executable = shutil.which("codex")
    if executable is None:
        raise FileNotFoundError("codex executable is not on PATH")
    result = subprocess.run(
        [executable, "queue", "--thread", thread_id, "--message", message],
        capture_output=True, text=True, timeout=30,
    )
    return {
        "threadId": thread_id, "exitCode": result.returncode,
        "stdout": result.stdout, "stderr": result.stderr,
        "status": "queued" if result.returncode == 0 else "failed",
        "deliveryConfirmed": False,
        "note": "No --resume is used; no second agent runtime is started.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["inspect", "serve", "queue"])
    parser.add_argument("--thread", required=True)
    parser.add_argument("--artifact-root", action="append")
    parser.add_argument("--codex-home")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--message-file")
    args = parser.parse_args()
    if args.command == "queue":
        if not args.message_file:
            parser.error("queue requires --message-file")
        print(json.dumps(enqueue(args.thread, args.message_file), ensure_ascii=False, indent=2))
        return
    if not args.artifact_root:
        parser.error("inspect and serve require --artifact-root")
    history = History(args.thread, args.artifact_root, args.codex_home)
    if args.command == "inspect":
        print(json.dumps({
            "session": history.snapshot(), "messages": history.messages(),
            "artifacts": history.artifacts(),
        }, ensure_ascii=False, indent=2))
        return
    token = os.environ.get("AIREMOTE_POC_TOKEN", "")
    server = ProbeServer(args.port, history, token)
    print(json.dumps({"listening": "http://127.0.0.1:%s" % server.server_port,
                      "threadId": history.thread_id, "mode": "read-only"}), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
