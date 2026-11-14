#!/usr/bin/env python3
"""Explicit live verification. Sends probes, edits its own queue items, creates a chat."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import secrets
import time
import uuid

from client import RelayClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--existing-thread', required=True)
    parser.add_argument('--active-thread', required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--report', default='evidence/public-web-verification.json')
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    client = RelayClient(args.origin, config['owner_token'])
    checks = []
    proof = {'origin': args.origin, 'verifiedAt': datetime.now(timezone.utc).isoformat(), 'checks': checks}
    report = Path(args.report)

    def check(name, condition, detail=None):
        record = {'name': name, 'passed': bool(condition)}
        if detail is not None: record['detail'] = detail
        checks.append(record)
        report.write_text(json.dumps(proof, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if not condition: raise AssertionError(name)

    def wait_reply(thread_id, marker, timeout=180):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            messages = client.bridge('GET', '/api/threads/' + thread_id + '/messages')['messages']
            matches = [m for m in messages if m['role'] == 'assistant' and marker in m['text']]
            if matches: return matches[-1], messages
            time.sleep(1)
        raise TimeoutError('No executed AI reply for ' + marker)

    try:
        status, headers, _ = client.http('/api/device', authenticated=False)
        check('unauthenticated history/control denied', status == 401)
        check('public HTTPS and response security headers', args.origin.startswith('https://') and headers.get('X-Content-Type-Options') == 'nosniff')
        check('Mac outbound connection online', client.api('/api/device')['connectorOnline'])
        threads = client.bridge('GET', '/api/threads')['threads']
        check('existing conversations through external relay', any(t['threadId'] == args.existing_thread for t in threads), {'threadCount': len(threads)})
        active = next(t for t in threads if t['threadId'] == args.active_thread)
        check('real desktop running status', active['runtimeStatus'].get('type') == 'active')
        capabilities = client.bridge('GET', '/api/capabilities')
        check('authorized project available for create', args.project in capabilities['workspaces'])
        probe_id, challenge = 'PUBLIC-' + secrets.token_hex(6), secrets.token_hex(24)
        path = Path(args.project) / 'poc-output' / (probe_id + '.json')
        expected = {'probeId': probe_id, 'challenge': challenge, 'threadId': args.existing_thread, 'execution': 'public-aliyun-relay-existing-desktop-thread'}
        text = '【AIRemote 公网网页闭环 ' + probe_id + '】这是用户授权的真实执行验证。请仅创建 ' + str(path) + '，JSON 内容为 ' + json.dumps(expected, ensure_ascii=False) + '。完成后回复 ACK ' + probe_id + ' 和文件绝对路径 Markdown 链接。不修改其他文件、不另开对话。'
        send_key = str(uuid.uuid4())
        receipt = client.bridge('POST', '/api/threads/' + args.existing_thread + '/messages', {'text': text}, send_key)
        check('same existing desktop owner accepted external instruction', receipt['result']['transport'] == 'original-desktop-ipc-owner', {'operationId': receipt['operationId'], 'threadId': receipt['result']['threadId']})
        retry = client.bridge('POST', '/api/threads/' + args.existing_thread + '/messages', {'text': text}, send_key)
        check('network retry returns same operation', receipt['operationId'] == retry['operationId'])
        reply, messages = wait_reply(args.existing_thread, 'ACK ' + probe_id)
        check('existing conversation actually executed and replied', path.is_file() and json.loads(path.read_text()) == expected,
              {'probeId': probe_id, 'replyItemId': reply['itemId'], 'file': str(path)})
        check('retry produced only one user message', len([m for m in messages if m['role'] == 'user' and probe_id in m['text']]) == 1)
        refreshed = client.bridge('GET', '/api/operations/' + receipt['operationId'])
        check('delivery reconciled against original persisted message', refreshed['delivery']['receivedInHistory'] and bool(refreshed['delivery']['laterAssistantMessages']))
        artifacts = client.bridge('GET', '/api/threads/' + args.existing_thread + '/artifacts')['artifacts']
        artifact = next(a for a in artifacts if a['name'] == path.name)
        status, headers, content, _ = client.bridge_raw('GET', '/api/threads/' + args.existing_thread + '/artifacts/' + artifact['id'] + '/download')
        check('public file download bytes match Mac original', status == 200 and content == path.read_bytes(),
              {'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content), 'contentDisposition': headers.get('Content-Disposition')})

        root = '/api/threads/' + args.active_thread + '/queue'
        own = []
        try:
            a = client.bridge('POST', root, {'text': '【AIRemote 公网队列管理临时验证 A ' + probe_id + '】测试完成后删除，不需要执行。'})['result']['item']
            own.append(a['id'])
            b = client.bridge('POST', root, {'text': '【AIRemote 公网队列管理临时验证 B ' + probe_id + '】测试完成后删除，不需要执行。'})['result']['item']
            own.append(b['id'])
            check('external native queue add/list', set(own).issubset({i['id'] for i in client.bridge('GET', root)['items']}))
            edited = '【AIRemote 公网队列编辑验证 ' + probe_id + '】测试完成后删除，不需要执行。'
            client.bridge('PATCH', root + '/' + b['id'], {'text': edited})
            current = client.bridge('GET', root)['items']
            check('external native queue edit', any(i['id'] == b['id'] and i['input'][0]['text'] == edited for i in current))
            ids = [i['id'] for i in current]
            at_a, at_b = ids.index(a['id']), ids.index(b['id']); ids[at_a], ids[at_b] = ids[at_b], ids[at_a]
            client.bridge('PUT', root + '/order', {'ids': ids})
            check('external native queue reorder', [i['id'] for i in client.bridge('GET', root)['items']] == ids)
            client.bridge('DELETE', root + '/' + a['id'])
            check('external native queue delete', a['id'] not in {i['id'] for i in client.bridge('GET', root)['items']})
            client.bridge('DELETE', root, {'ids': [b['id']]})
            check('external native queue clear selected', not set(own).intersection({i['id'] for i in client.bridge('GET', root)['items']}))
        finally:
            left = {i['id'] for i in client.bridge('GET', root)['items']}.intersection(own)
            if left: client.bridge('DELETE', root, {'ids': sorted(left)})

        create_marker = 'PUBLIC_CREATE_ACK ' + secrets.token_hex(12)
        create_key = str(uuid.uuid4())
        create_body = {'cwd': args.project, 'title': 'AIRemote 阿里云公网新建验证', 'text': '用户授权的新建对话验证。请仅回复 ' + create_marker + '，不调用工具，不修改文件，不创建其他对话。'}
        created = client.bridge('POST', '/api/threads', create_body, create_key)
        new_id = created['result']['threadId']; proof['createdThreadId'] = new_id
        repeated = client.bridge('POST', '/api/threads', create_body, create_key)
        check('external creates one real new Codex chat', created['result']['isNewThread'] and repeated['result']['threadId'] == new_id, {'threadId': new_id})
        reply, _ = wait_reply(new_id, create_marker)
        check('external new conversation actually replied', create_marker in reply['text'])

        queue_probe, queue_challenge = 'PUBLIC-QUEUE-' + secrets.token_hex(6), secrets.token_hex(24)
        queue_path = Path(args.project) / 'poc-output' / (queue_probe + '.json')
        queue_expected = {'probeId': queue_probe, 'challenge': queue_challenge, 'threadId': args.active_thread, 'execution': 'public-aliyun-native-queue-send-now'}
        prompt = '【AIRemote 公网原生队列立即执行验证 ' + queue_probe + '】请继续当前 AIRemote 手机网页任务，并创建 ' + str(queue_path) + '，JSON 内容为 ' + json.dumps(queue_expected, ensure_ascii=False) + '。完成后在本对话发进度消息 ACK ' + queue_probe + ' 和文件绝对路径 Markdown 链接，再继续原任务。仅写这个测试文件，不修改其他项目，不提前结束当前任务。'
        queued = client.bridge('POST', root, {'text': prompt})['result']['item']
        sent = client.bridge('POST', root + '/' + queued['id'] + '/send', {})
        proof['queueSendNow'] = {'operationId': sent['operationId'], 'queueId': queued['id'], 'probeId': queue_probe}
        report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
        check('external queue send-now accepted by same desktop owner', sent['result']['transport'] == 'original-desktop-ipc-owner')
        reply, _ = wait_reply(args.active_thread, 'ACK ' + queue_probe)
        check('external queue send-now truly received and executed', queue_path.is_file() and json.loads(queue_path.read_text()) == queue_expected,
              {'probeId': queue_probe, 'replyItemId': reply['itemId'], 'file': str(queue_path)})
        check('executed queue item no longer pending', queued['id'] not in {i['id'] for i in client.bridge('GET', root)['items']})
        proof['passed'] = all(c['passed'] for c in checks)
    except Exception as error:
        proof['passed'] = False; proof['error'] = str(error)
        raise
    finally:
        proof['finishedAt'] = datetime.now(timezone.utc).isoformat()
        report.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n')
        print(json.dumps({'report': str(report.resolve()), 'passed': proof.get('passed'), 'checks': len(checks)}), flush=True)


if __name__ == '__main__':
    main()
