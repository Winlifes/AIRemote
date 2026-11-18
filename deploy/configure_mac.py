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


def relay_origin(value):
    if not isinstance(value, str):
        raise ValueError('请输入 HTTPS 服务器地址。')
    origin = value.strip().rstrip('/')
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError('请输入完整的 HTTPS 服务器地址，例如 https://remote.example.com。')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('服务器端口无效。')
    return origin


def configure(home, origin, agent_token, workspaces, threads=(), port=8765, name='My Mac'):
    home = Path(home).expanduser().resolve()
    private = home / '.local'
    if (private / 'mac-runtime.json').exists():
        raise ValueError('Configuration already exists; refusing to replace credentials')
    if len(agent_token) < 24:
        raise ValueError('Relay agent token is missing or too short')
    roots = [str(Path(path).expanduser().resolve(strict=True)) for path in workspaces]
    if any(not Path(path).is_dir() for path in roots):
        raise ValueError('Workspaces must be existing directories')
    private.mkdir(parents=True, exist_ok=True, mode=0o700); private.chmod(0o700)
    token = private / 'access-token'
    fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as output:
        output.write(secrets.token_urlsafe(32) + '\n')
    agent = private / 'relay-agent.json'
    private_json(agent, {'relay_url': origin, 'agent_token': agent_token,
        'connector_url': 'http://127.0.0.1:' + str(port), 'connector_token_file': str(token),
        'device_name': name})
    config = private / 'mac-runtime.json'
    private_json(config, {'project': str(home), 'runtime_root': str(Path(__file__).resolve().parents[1]),
        'threads': list(threads), 'artifact_roots': roots, 'workspace_roots': roots,
        'discover_desktop_projects': True, 'token_file': str(token), 'connector_state': str(private / 'connector-state'),
        'agent_config': str(agent), 'connector_port': port, 'relay_url': origin})
    return config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / 'Library/Application Support/AIRemote')
    parser.add_argument('--credentials', type=Path, help='Private credentials exported by setup_relay.py')
    parser.add_argument('--relay-url')
    parser.add_argument('--workspace', action='append', required=True, type=Path)
    parser.add_argument('--thread', action='append', default=[])
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    credentials = json.loads(args.credentials.read_text()) if args.credentials else {
        'relay_url': args.relay_url, 'agent_token': getpass.getpass('Relay agent token (hidden): ')}
    origin = relay_origin(credentials.get('relay_url') or args.relay_url)
    config = configure(args.home, origin, credentials.get('agent_token', ''), args.workspace, args.thread, args.port)
    print(json.dumps({'configured': True, 'home': str(config.parent.parent), 'config': str(config)}, ensure_ascii=False))


if __name__ == '__main__': main()
