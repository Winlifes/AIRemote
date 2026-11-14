#!/usr/bin/env python3
"""Install an isolated release on the user's ECS; preserve other Caddy hosts."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release', required=True)
    parser.add_argument('--config', required=True)
    parser.add_argument('--domain', required=True)
    parser.add_argument('--auto-update', action='store_true', help='Enable stable GitHub release updates every six hours')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('Installation requires root')
    release = Path(args.release).resolve(strict=True)
    if not release.is_relative_to(Path('/opt/airemote/releases')):
        raise ValueError('Release must be below /opt/airemote/releases')
    try:
        account = pwd.getpwnam('airemote')
    except KeyError:
        run('useradd', '--system', '--home', '/var/lib/airemote', '--shell', '/usr/sbin/nologin', 'airemote')
        account = pwd.getpwnam('airemote')
    for directory in ['/etc/airemote', '/var/lib/airemote']:
        path = Path(directory); path.mkdir(parents=True, exist_ok=True); path.chmod(0o700); os.chown(path, account.pw_uid, account.pw_gid)
    target = Path('/etc/airemote/relay.json')
    source = Path(args.config)
    if source.resolve() != target.resolve():
        shutil.copyfile(source, target)
    target.chmod(0o600); os.chown(target, account.pw_uid, account.pw_gid)
    unit = Path('/etc/systemd/system/airemote.service')
    unit.write_text((release / 'deploy/airemote.service').read_text())
    old = Path('/opt/airemote/current')
    previous = str(old.resolve()) if old.is_symlink() else None
    temporary = old.with_name('current.next')
    temporary.unlink(missing_ok=True); temporary.symlink_to(release); os.replace(temporary, old)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'enable', '--now', 'airemote')
    run('systemctl', 'restart', 'airemote')
    caddy = Path('/etc/caddy/Caddyfile')
    original = caddy.read_text()
    marker = '# AIRemote private relay'
    if marker not in original:
        if args.domain in original:
            raise RuntimeError('Domain already configured; inspect before changing it')
        block = '\n' + marker + '\n' + args.domain + ' {\n    encode zstd gzip\n    header Strict-Transport-Security "max-age=31536000"\n    request_body {\n        max_size 65MB\n    }\n    reverse_proxy 127.0.0.1:8787\n}\n'
        temporary = caddy.with_name('Caddyfile.airemote-next')
        temporary.write_text(original + block)
        run('caddy', 'validate', '--config', str(temporary), '--adapter', 'caddyfile')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = caddy.with_name('Caddyfile.before-airemote-' + stamp)
        shutil.copy2(caddy, backup)
        os.replace(temporary, caddy)
        try:
            run('systemctl', 'reload', 'caddy')
        except BaseException:
            shutil.copy2(backup, caddy)
            run('systemctl', 'reload', 'caddy')
            raise
    if args.auto_update:
        for name in ['airemote-update.service', 'airemote-update.timer']:
            shutil.copyfile(release / 'deploy' / name, Path('/etc/systemd/system') / name)
        run('systemctl', 'daemon-reload')
        run('systemctl', 'enable', '--now', 'airemote-update.timer')
    print(json.dumps({'release': str(release), 'previous': previous, 'service': run('systemctl', 'is-active', 'airemote'), 'domain': args.domain, 'autoUpdateEnabled': args.auto_update, 'caddy': 'validated and reloaded'}))


if __name__ == '__main__':
    main()
