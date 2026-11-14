#!/usr/bin/env python3
"""Generate an HTTPS relay configuration and private client setup files."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'relay'))
from server import initialize_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--origin', required=True)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.scheme != 'https' or not origin.hostname or origin.username or origin.path not in {'', '/'} or origin.query or origin.fragment:
        raise ValueError('An HTTPS server origin is required')
    config, password = initialize_config(args.config, args.origin.rstrip('/'))
    credentials = args.config.with_name('mac-agent-credentials.json')
    fd = os.open(credentials, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream: json.dump({'relay_url': args.origin.rstrip('/'), 'agent_token': config['agent_token']}, stream)
    login = args.config.with_name('phone-login.txt')
    fd = os.open(login, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream: stream.write('username: ' + config['username'] + '\npassword: ' + password + '\n')
    print(json.dumps({'config': str(args.config), 'macCredentials': str(credentials), 'phoneLogin': str(login), 'credentialsPrinted': False}))


if __name__ == '__main__': main()
