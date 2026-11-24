#!/usr/bin/env python3
"""Package only the public relay runtime and built web assets."""
import argparse
from pathlib import Path
import tarfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=root / 'dist/AIRemote-server.tar.gz')
    args = parser.parse_args()
    if not (root / 'web/dist/index.html').is_file(): raise ValueError('Build web assets first')
    files = [root / name for name in ['VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md', 'relay/server.py', 'relay/events.py', 'relay/protocol.py', 'relay/accounts.py', 'relay/routing.py',
        'deploy/install.py', 'deploy/setup_relay.py', 'deploy/update_core.py', 'deploy/update_server.py',
        'deploy/airemote.service', 'deploy/airemote-update.service', 'deploy/airemote-update.timer']]
    files += sorted(path for path in (root / 'web/dist').rglob('*') if path.is_file())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(args.output, 'w:gz') as archive:
        for path in files:
            if path.is_symlink(): raise ValueError('Unexpected symlink')
            info = archive.gettarinfo(str(path), str(path.relative_to(root)))
            info.uid = info.gid = info.mtime = 0; info.uname = info.gname = ''; info.mode = 0o644
            with path.open('rb') as source: archive.addfile(info, source)
    print(args.output)


if __name__ == '__main__': main()
