#!/usr/bin/env python3
"""Package the Windows launcher and private-data-free Python runtime."""

import argparse
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FILES = [
    'VERSION', 'README.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'windows/app.py', 'windows/connector.py', 'poc/codex_probe.py', 'poc/codex_rollout.py',
    'relay/agent.py', 'relay/protocol.py', 'relay/local_lock.py',
    'deploy/pair_mac.py', 'deploy/configure_mac.py',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/AIRemote-windows.zip')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, 'w', zipfile.ZIP_DEFLATED) as package:
        for name in FILES:
            package.write(ROOT / name, 'AIRemote-windows/' + name)
        command = (ROOT / 'windows/AIRemote.cmd').read_text(encoding='ascii').replace('\n', '\r\n')
        package.writestr('AIRemote-windows/AIRemote.cmd', command.encode('ascii'))
    print(args.output)


if __name__ == '__main__':
    main()
