#!/usr/bin/env python3
"""Audit the Git publication set without printing sensitive values."""
import argparse
import json
from pathlib import Path
import re
import subprocess


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--secrets', type=Path, help='Optional private JSON array of values to exclude')
    args = parser.parse_args()
    known = json.loads(args.secrets.read_text()) if args.secrets else []
    patterns = [re.compile(rb'gh[pousr]_[A-Za-z0-9_]{24,}'),
                re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----')]
    names = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
    errors = []
    for name in filter(None, names):
        p = Path(name)
        if any(part in {'.local', 'evidence', 'poc-output', 'node_modules', 'build', 'dist', '.gradle'} for part in p.parts) or p.name in {'design-qa.md', 'local.properties'} or p.suffix in {'.jks', '.keystore', '.pem', '.key', '.env'}:
            errors.append((name, 'private/generated path')); continue
        if not p.is_file(): continue
        content = p.read_bytes()
        if any(pattern.search(content) for pattern in patterns): errors.append((name, 'credential pattern'))
        if any(value and value.encode() in content for value in known): errors.append((name, 'private value'))
    if errors:
        print(json.dumps({'passed': False, 'findings': errors})); raise SystemExit(1)
    print(json.dumps({'passed': True, 'files': len(list(filter(None, names))), 'sensitiveValuesPrinted': False}))


if __name__ == '__main__': main()
