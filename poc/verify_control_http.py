#!/usr/bin/env python3
"""Real HTTP E2E checks. Writes probes only into this project's selected thread.

This intentionally sends a new instruction to the existing desktop conversation
and creates one genuine new Codex thread. Run only when that test is authorized.
"""

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

from control_server import Controller, ControlServer


ROOT = Path(__file__).resolve().parents[1]



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--thread", required=True, help="Existing authorized desktop chat UUID")
    args = parser.parse_args()
    THREAD = str(uuid.UUID(args.thread))
    (ROOT / "poc-output").mkdir(exist_ok=True)
    (ROOT / "evidence").mkdir(exist_ok=True)
    probe = "HTTP-" + secrets.token_hex(6)
    challenge = secrets.token_hex(24)
    artifact = ROOT / "poc-output" / (probe + ".json")
    record = {"probeId": probe, "challenge": challenge, "threadId": THREAD,
              "artifactPath": str(artifact), "checkedAt": datetime.now().astimezone().isoformat(), "checks": []}
    evidence = ROOT / "evidence/http-control-verification.json"
    evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    controller = Controller([THREAD], [ROOT / "poc-output"], [ROOT], ROOT / ".local/http-live-proof")
    token = secrets.token_urlsafe(32)
    server = ControlServer(0, controller, token)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = "http://127.0.0.1:%s" % server.server_port
    queue_path = "/api/threads/%s/queue" % THREAD
    created_queue_ids = []

    def call(method, path, body=None, key=None, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = "Bearer " + token
        if method != "GET":
            headers["Idempotency-Key"] = key or str(uuid.uuid4())
        request = Request(base + path, headers=headers, method=method,
                          data=None if method == "GET" else json.dumps(body or {}, ensure_ascii=False).encode())
        with urlopen(request, timeout=40) as response:
            return json.load(response)

    def check(name, passed, detail=None):
        record["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        if not passed:
            raise AssertionError(name)

    try:
        session = call("GET", "/api/threads/" + THREAD)
        check("live_original_owner", session.get("runtimeSource") == "original desktop IPC owner", {
            "runtimeStatus": session["runtimeStatus"], "ownerClientId": session["ownerClientId"],
        })
        try:
            call("POST", queue_path, {"text": "unauthorized probe"}, auth=False)
            check("reject_unauthenticated_write", False)
        except HTTPError as error:
            check("reject_unauthenticated_write", error.code == 401, error.code)
        baseline = call("GET", queue_path)["items"]
        item_key = "queue-add-" + probe
        first = call("POST", queue_path, {"text": probe + " queue A"}, item_key)
        first_again = call("POST", queue_path, {"text": probe + " queue A"}, item_key)
        check("queue_add_idempotent", first["result"]["item"]["id"] == first_again["result"]["item"]["id"] and first_again["replayed"])
        a = first["result"]["item"]["id"]
        b = call("POST", queue_path, {"text": probe + " queue B"})["result"]["item"]["id"]
        c = call("POST", queue_path, {"text": probe + " queue C"})["result"]["item"]["id"]
        created_queue_ids.extend([a, b, c])
        call("PATCH", queue_path + "/" + b, {"text": probe + " B edited"})
        items = call("GET", queue_path)["items"]
        check("queue_edit", any(i["id"] == b and i["input"][0]["text"] == probe + " B edited" for i in items))
        order = [i["id"] for i in baseline] + [c, b, a]
        call("PUT", queue_path + "/order", {"ids": order})
        check("queue_reorder", [i["id"] for i in call("GET", queue_path)["items"]] == order)
        call("DELETE", queue_path + "/" + a)
        check("queue_delete", a not in {i["id"] for i in call("GET", queue_path)["items"]})
        call("DELETE", queue_path, {"ids": [b, c]})
        check("queue_clear_selected_preserves_baseline", call("GET", queue_path)["items"] == baseline)

        new_nonce = secrets.token_hex(12)
        new_payload = {"cwd": str(ROOT), "title": "AIRemote HTTP 新建对话验证",
                       "text": "用户授权的新建对话验证。请只回复 HTTP_CREATE_ACK " + probe + " " + new_nonce + "，不调用工具。"}
        create_key = "new-thread-" + probe
        created = call("POST", "/api/threads", new_payload, create_key)
        created_again = call("POST", "/api/threads", new_payload, create_key)
        new_thread = created["result"]["threadId"]
        record["newThreadId"] = new_thread
        check("create_thread_idempotent", new_thread == created_again["result"]["threadId"] and created_again["replayed"], new_thread)

        payload = {"probeId": probe, "challenge": challenge, "threadId": THREAD, "execution": "http-native-queue-send-now"}
        prompt = ("【AIRemote HTTP 写入闭环 " + probe + "】请继续当前任务，并创建 " + str(artifact)
                  + "，JSON 内容为 " + json.dumps(payload, ensure_ascii=False)
                  + "。随后在本对话发进度消息 ACK " + probe + " 和文件绝对路径 Markdown 链接，再继续完成验证和交付。仅创建该测试文件，不修改其他项目，不提前结束原任务。")
        queued = call("POST", queue_path, {"text": prompt})["result"]["item"]
        created_queue_ids.append(queued["id"])
        record["queueId"] = queued["id"]
        send_key = "send-now-" + probe
        sent = call("POST", queue_path + "/" + queued["id"] + "/send", {}, send_key)
        sent_again = call("POST", queue_path + "/" + queued["id"] + "/send", {}, send_key)
        record["sendOperationId"] = sent["operationId"]
        record["desktopReceipt"] = sent["result"]["desktopReceipt"]
        check("send_now_idempotent", sent["operationId"] == sent_again["operationId"] and sent_again["replayed"])
        check("queue_item_consumed", queued["id"] not in {i["id"] for i in call("GET", queue_path)["items"]})
        print(json.dumps({"probeId": probe, "existingThread": THREAD, "newThreadId": new_thread,
                          "waitingForExistingThreadExecution": True}, ensure_ascii=False), flush=True)

        deadline = time.monotonic() + 120
        receipt = None
        while time.monotonic() < deadline:
            receipt = call("GET", "/api/operations/" + sent["operationId"])
            messages = receipt.get("delivery", {}).get("laterAssistantMessages", [])
            ack = any(m["text"].startswith("ACK " + probe) for m in messages)
            if artifact.exists() and ack:
                break
            time.sleep(0.3)
        check("instruction_received_by_same_thread", receipt.get("delivery", {}).get("receivedInHistory"), receipt.get("delivery"))
        check("instruction_executed_challenge_matches", artifact.exists() and json.loads(artifact.read_text())["challenge"] == challenge)
        check("ack_read_back_over_http", ack)
        record["executionConfirmed"] = True
        record["receipt"] = receipt
        records = controller.history(THREAD).messages(1000)
        client_id = sent["result"]["clientUserMessageId"]
        check("one_user_message_after_repeated_send", sum(m.get("clientId") == client_id for m in records) == 1)

        artifacts = call("GET", "/api/threads/%s/artifacts" % THREAD)["artifacts"]
        linked = next(i for i in artifacts if i["path"] == str(artifact))
        request = Request(base + "/api/threads/%s/artifacts/%s/download" % (THREAD, linked["id"]),
                          headers={"Authorization": "Bearer " + token})
        with urlopen(request, timeout=10) as response:
            downloaded = response.read()
        destination = ROOT / "evidence/downloads" / artifact.name
        destination.write_bytes(downloaded)
        check("download_executed_artifact", downloaded == artifact.read_bytes(), hashlib.sha256(downloaded).hexdigest())

        deadline = time.monotonic() + 90
        new_messages = []
        while time.monotonic() < deadline:
            new_messages = call("GET", "/api/threads/%s/messages" % new_thread)["messages"]
            if any(m["role"] == "assistant" and "HTTP_CREATE_ACK " + probe + " " + new_nonce in m["text"] for m in new_messages):
                break
            time.sleep(0.3)
        check("new_thread_received_and_replied", any(m["role"] == "assistant" and "HTTP_CREATE_ACK " + probe + " " + new_nonce in m["text"] for m in new_messages))
        record["newThreadAck"] = [m for m in new_messages if m["role"] == "assistant"]
        record["allPassed"] = all(c["passed"] for c in record["checks"])
        evidence.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"allPassed": record["allPassed"], "checks": len(record["checks"]),
                          "newThreadId": new_thread, "executionConfirmed": record["executionConfirmed"],
                          "evidence": str(evidence)}, ensure_ascii=False), flush=True)
    finally:
        try:
            remaining = {i["id"] for i in controller.queue.list(THREAD)}
            for item_id in created_queue_ids:
                if item_id in remaining:
                    controller.queue.delete(THREAD, item_id)
        finally:
            server.shutdown()
            worker.join(timeout=3)
            server.server_close()
            controller.close()


if __name__ == "__main__":
    main()
