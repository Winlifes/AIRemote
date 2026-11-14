#!/usr/bin/env python3
"""Record request paths around browser QA without recording chat contents/secrets."""
import argparse
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import parse_qs, urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['mark', 'finish'])
    parser.add_argument('stage')
    parser.add_argument('--mode', choices=['projects', 'project', 'none', 'chat', 'history'])
    parser.add_argument('--id')
    parser.add_argument('--provider', choices=['codex', 'deepseek'], default='codex')
    parser.add_argument('--state', default='.local/relay-local-state')
    parser.add_argument('--report', default='evidence/lazy-ui-network.json')
    args = parser.parse_args()
    report = Path(args.report)
    document = json.loads(report.read_text()) if report.exists() else {'stages': {}}
    if args.action == 'mark':
        document['stages'][args.stage] = {'startedAt': time.time()}
        report.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n')
        print(json.dumps({'marked': args.stage})); return
    stage = document['stages'][args.stage]
    db = sqlite3.connect(Path(args.state) / 'relay.sqlite'); db.row_factory = sqlite3.Row
    events = []
    for row in db.execute('SELECT id,request,state,status FROM jobs WHERE created>=? ORDER BY created', (stage['startedAt'],)):
        request = json.loads(row['request']); path = request.get('path', '')
        if not path: continue
        parts = urlsplit(path); query = parse_qs(parts.query)
        event = {'path': parts.path, 'method': request['method'], 'hasBeforeCursor': 'before' in query,
                 'limit': query.get('limit', [None])[0], 'state': row['state'], 'status': row['status']}
        blob = Path(args.state) / 'responses' / row['id']
        if parts.path.endswith('/messages') and blob.is_file() and row['status'] == 200:
            body = json.loads(blob.read_text()); event['returnedMessages'] = len(body.get('messages', []))
        events.append(event)
    metadata = [event for event in events if event['path'] == '/api/providers']
    events = [event for event in events if event['path'] != '/api/providers']
    prefix = '/api' if args.provider == 'codex' else '/api/providers/deepseek'
    if args.mode == 'projects':
        passed = bool(events) and all(e['path'] == prefix + '/projects' for e in events)
    elif args.mode == 'project':
        passed = bool(events) and all(e['path'] == prefix + '/projects/' + args.id + '/threads' for e in events)
    elif args.mode == 'none':
        passed = not events
    elif args.mode in {'chat', 'history'}:
        root = prefix + '/threads/' + args.id
        passed = bool(events) and all(e['path'] in {root, root + '/messages', root + '/queue'} for e in events)
        messages = [event for event in events if event['path'].endswith('/messages')]
        passed = passed and bool(messages) and all(event['limit'] == '40' and event.get('returnedMessages', 0) <= 40 for event in messages)
        passed = passed and (any(event['hasBeforeCursor'] for event in messages) if args.mode == 'history' else not any(event['hasBeforeCursor'] for event in messages))
    else:
        passed = False
    passed = passed and all(event['status'] is None or event['status'] < 400 for event in events)
    stage.update({'finishedAt': time.time(), 'events': events, 'connectionMetadataRequests': len(metadata), 'provider': args.provider, 'mode': args.mode, 'passed': bool(passed)})
    report.write_text(json.dumps(document, indent=2, ensure_ascii=False) + '\n')
    print(json.dumps({'stage': args.stage, 'passed': bool(passed), 'requests': events}, ensure_ascii=False))
    if not passed: raise SystemExit(1)


if __name__ == '__main__': main()
