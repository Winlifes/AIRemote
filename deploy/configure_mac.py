#!/usr/bin/env python3
"""Create private Mac runtime configuration; never print its credentials."""
import argparse
import getpass
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlsplit


def private_json(path, value):
    path = Path(path); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True); path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output: json.dump(value, output, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / 'Library/Application Support/AIRemote')
    parser.add_argument('--credentials', type=Path, help='Private credentials exported by setup_relay.py')
    parser.add_argument('--relay-url')
    parser.add_argument('--workspace', action='append', required=True, type=Path)
    parser.add_argument('--thread', action='append', default=[])
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    home = args.home.expanduser().resolve(); private = home / '.local'
    if (private / 'mac-runtime.json').exists(): raise ValueError('Configuration already exists; refusing to replace credentials')
    credentials = json.loads(args.credentials.read_text()) if args.credentials else {
        'relay_url': args.relay_url, 'agent_token': getpass.getpass('Relay agent token (hidden): ')}
    origin = credentials.get('relay_url') or args.relay_url or ''
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.path not in {'', '/'} or parsed.query or parsed.fragment:
        raise ValueError('Relay must be an HTTPS origin')
    if len(credentials.get('agent_token', '')) < 24: raise ValueError('Relay agent token is missing or too short')
    roots = [str(path.expanduser().resolve(strict=True)) for path in args.workspace]
    if any(not Path(path).is_dir() for path in roots): raise ValueError('Workspaces must be existing directories')
    private.mkdir(parents=True, exist_ok=True, mode=0o700); private.chmod(0o700)
    token = private / 'access-token'
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output: output.write(secrets.token_urlsafe(32) + '\n')
    agent = private / 'relay-agent.json'
    private_json(agent, {'relay_url': origin.rstrip('/'), 'agent_token': credentials['agent_token'],
        'connector_url': 'http://127.0.0.1:' + str(args.port), 'connector_token_file': str(token),
        'device_name': 'My Mac'})
    private_json(private / 'mac-runtime.json', {'project': str(home), 'runtime_root': str(Path(__file__).resolve().parents[1]),
        'threads': args.thread, 'artifact_roots': roots, 'workspace_roots': roots,
        'discover_desktop_projects': True, 'token_file': str(token), 'connector_state': str(private / 'connector-state'),
        'agent_config': str(agent), 'connector_port': args.port, 'relay_url': origin.rstrip('/')})
    print(json.dumps({'configured': True, 'home': str(home), 'config': str(private / 'mac-runtime.json')}, ensure_ascii=False))


if __name__ == '__main__': main()
