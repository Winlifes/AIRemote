#!/usr/bin/env python3
"""Update an installed relay from GitHub; atomically switch and health-check."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
from urllib.request import build_opener, ProxyHandler
import uuid

from update_core import DEFAULT_REPO, extract_tar, fetch_asset, latest_release, release_asset, version_tuple


def restart():
    subprocess.run(['systemctl', 'restart', 'airemote'], check=True, capture_output=True)


def healthy(version):
    try:
        with build_opener(ProxyHandler({})).open('http://127.0.0.1:8787/api/health', timeout=2) as response:
            value = json.load(response)
            return value.get('ok') is True and value.get('version') == version
    except (OSError, ValueError): return False


def switch_release(release, current, version, restarter=restart, health=healthy, timeout=25):
    release, current = Path(release), Path(current)
    if not current.is_symlink(): raise ValueError('Existing current release must be a symlink')
    previous = current.resolve(strict=True)
    temporary = current.with_name('current.update-next')
    temporary.unlink(missing_ok=True)
    try:
        temporary.symlink_to(release); os.replace(temporary, current)
        restarter()
        deadline = time.monotonic() + timeout
        while True:
            if health(version): return {'installed': True, 'version': version, 'previous': str(previous)}
            if time.monotonic() >= deadline: raise RuntimeError('New relay failed its versioned health check')
            time.sleep(.5)
    except BaseException:
        temporary.unlink(missing_ok=True); temporary.symlink_to(previous); os.replace(temporary, current)
        restarter()
        raise


def update(root=Path('/opt/airemote'), repo=DEFAULT_REPO, apply=False):
    root = Path(root).resolve(strict=True)
    current = root / 'current'
    old = (current / 'VERSION').read_text().strip() if (current / 'VERSION').is_file() else '0.0.0'
    release = latest_release(repo); new = release['tag_name'].lstrip('v')
    result = {'currentVersion': old, 'latestVersion': new, 'available': version_tuple(new) > version_tuple(old)}
    if not apply or not result['available']: return result
    if os.geteuid() != 0: raise PermissionError('Relay updates require root')
    stage = root / '.updates' / uuid.uuid4().hex
    stage.mkdir(parents=True, mode=0o700)
    try:
        archive = fetch_asset(release_asset(release, 'AIRemote-server.tar.gz', repo), stage)
        candidate = stage / 'unpacked'; extract_tar(archive, candidate)
        if (candidate / 'VERSION').read_text().strip() != new: raise ValueError('Archive version does not match release')
        for filename in ['relay/server.py', 'relay/protocol.py', 'web/dist/index.html', 'deploy/update_server.py']:
            if not (candidate / filename).is_file(): raise ValueError('Incomplete relay release')
        target = root / 'releases' / ('v' + new)
        if target.exists(): raise ValueError('Release directory already exists; inspect previous attempt before retrying')
        # The service user can read public code; credentials and data remain elsewhere.
        for directory in [candidate, *[path for path in candidate.rglob('*') if path.is_dir()]]: directory.chmod(0o755)
        target.parent.mkdir(parents=True, exist_ok=True); os.replace(candidate, target)
        return {**result, **switch_release(target, current, new)}
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--root', type=Path, default=Path('/opt/airemote'))
    parser.add_argument('--repo', default=DEFAULT_REPO)
    args = parser.parse_args()
    if args.apply:
        if os.geteuid() != 0: raise PermissionError('Relay updates require root')
        with open('/run/lock/airemote-update.lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = update(args.root, args.repo, True)
    else: result = update(args.root, args.repo)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'error': str(error)}), flush=True); raise SystemExit(1)
