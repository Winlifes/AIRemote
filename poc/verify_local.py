#!/usr/bin/env python3
"""Exercise the real desktop history and download its linked test artifacts."""

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import threading
from datetime import datetime
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from codex_probe import History, ProbeServer


ROOT = Path(__file__).resolve().parents[1]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True)
    parser.add_argument("--expect-status", choices=["inProgress", "completed", "interrupted", "failed"])
    args = parser.parse_args()
    THREAD_ID = args.thread
    history = History(THREAD_ID, [ROOT / "poc-output"])
    token = secrets.token_urlsafe(32)
    server = ProbeServer(0, history, token)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = "http://127.0.0.1:%s" % server.server_port
    evidence = ROOT / "evidence"
    downloads = evidence / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)

    def get(path, authenticate=True):
        headers = {"Authorization": "Bearer " + token} if authenticate else {}
        with urlopen(Request(base + path, headers=headers), timeout=15) as response:
            return response.status, dict(response.headers), response.read()

    results = {
        "checkedAt": datetime.now().astimezone().isoformat(),
        "threadId": THREAD_ID,
        "transport": "loopback HTTP + read-only Codex SQLite; no second agent",
        "checks": [],
    }

    def check(name, passed, detail):
        results["checks"].append({"name": name, "passed": bool(passed), "detail": detail})

    try:
        _, _, body = get("/api/session")
        snapshot = json.loads(body)
        results["session"] = snapshot
        check("same_existing_thread", snapshot["threadId"] == THREAD_ID, snapshot["title"])
        observed_status = snapshot["latestPersistedTurnStatus"]
        expected = args.expect_status
        valid_status = observed_status == expected if expected else observed_status in {
            "inProgress", "completed", "interrupted", "failed",
        }
        check("persisted_turn_status", valid_status,
              {"observed": observed_status, "expected": expected})
        check("completed_turn_status", any(t["status"] == "completed" for t in snapshot["turns"]),
              [{"turnId": t["turn_id"], "status": t["status"]} for t in snapshot["turns"]])

        _, _, body = get("/api/messages")
        messages = json.loads(body)["messages"]
        finals = [m for m in messages if m["role"] == "assistant" and m["phase"] == "final_answer"]
        check("read_completed_reply", bool(finals),
              [{"itemId": m["itemId"], "turnId": m["turnId"], "textLength": len(m["text"])} for m in finals])
        acks = [m for m in messages if m["role"] == "assistant" and "ACK NATIVE-20261111-A" in m["text"]]
        check("read_new_ack_in_same_thread", bool(acks),
              [{"threadId": m["threadId"], "turnId": m["turnId"], "itemId": m["itemId"], "text": m["text"]} for m in acks])

        _, _, body = get("/api/artifacts")
        artifacts = json.loads(body)["artifacts"]
        expected = {"native-takeover.txt", "native-takeover.zip"}
        selected = [a for a in artifacts if a["name"] in expected]
        check("associate_generated_files", {a["name"] for a in selected} == expected, selected)
        for artifact in selected:
            _, headers, data = get("/api/artifacts/%s/download" % artifact["id"])
            destination = downloads / artifact["name"]
            destination.write_bytes(data)
            received_hash = hashlib.sha256(data).hexdigest()
            original_hash = hashlib.sha256(Path(artifact["path"]).read_bytes()).hexdigest()
            check("http_download_" + artifact["name"], received_hash == original_hash, {
                "bytes": len(data), "sha256": received_hash,
                "sourceSha256": original_hash, "savedAs": str(destination),
                "contentDisposition": headers.get("Content-Disposition"),
            })
        # A new HTTP connection reads the same thread again; this is a read
        # reconnect check, not a claim of exactly-once instruction delivery.
        _, _, body = get("/api/session")
        check("read_reconnect", json.loads(body)["threadId"] == THREAD_ID, "New HTTP request after downloads")
        try:
            get("/api/messages", authenticate=False)
            check("reject_unauthenticated_read", False, "Unexpected HTTP 200")
        except HTTPError as error:
            check("reject_unauthenticated_read", error.code == 401, error.code)

        results["readAndDownloadChecksPassed"] = all(c["passed"] for c in results["checks"])
        results["limits"] = [
            "Local SQL history does not prove runtime liveness or waiting-on-approval state.",
            "Native write succeeded through Codex's in-app tool, not through this HTTP API.",
            "The separate official CLI queue test is not a confirmed delivery.",
            "Public-network/mobile transport and idle-session wake-up have not been tested.",
        ]
        (evidence / "read-download-verification.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(results, ensure_ascii=False, indent=2))
        if not results["readAndDownloadChecksPassed"]:
            raise SystemExit(1)
    finally:
        server.shutdown()
        worker.join(timeout=3)
        server.server_close()


if __name__ == "__main__":
    main()
