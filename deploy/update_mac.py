#!/usr/bin/env python3
"""Stage a verified GitHub Mac release, then replace the app with rollback."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import time
import uuid

from start_mac import start_services
from update_core import DEFAULT_REPO, extract_zip, fetch_asset, latest_release, release_asset, version_tuple, progress_event

BUNDLE_ID = 'fun.winlife.airemote'


def app_info(app):
    return plistlib.loads((Path(app) / 'Contents/Info.plist').read_bytes())


def runtime(app, config):
    bundled = Path(app) / 'Contents/Resources/runtime'
    if bundled.is_dir(): return bundled
    cfg = json.loads(Path(config).read_text())
    return Path(cfg.get('runtime_root') or cfg['project'])


def verify_bundle(app, version):
    info = app_info(app)
    if info.get('CFBundleIdentifier') != BUNDLE_ID or info.get('CFBundleShortVersionString') != version.lstrip('v'):
        raise ValueError('Downloaded app identity/version does not match the release')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True, capture_output=True)


def check(current, repo=DEFAULT_REPO, progress=None):
    release = latest_release(repo, progress=progress)
    return release, {'currentVersion': current, 'latestVersion': release['tag_name'].lstrip('v'),
        'available': version_tuple(release['tag_name']) > version_tuple(current),
        'releaseUrl': release['html_url']}


def validate_target(app):
    app = Path(app).expanduser().absolute()
    if app.name != 'AIRemote.app' or app.parent not in {Path('/Applications'), Path.home() / 'Applications'}:
        raise ValueError('Only /Applications/AIRemote.app or ~/Applications/AIRemote.app can be updated')
    if app.is_symlink(): raise ValueError('Refusing to replace a symlinked app')
    if app.exists() and app_info(app).get('CFBundleIdentifier') != BUNDLE_ID:
        raise ValueError('Refusing to replace an unrelated application')
    return app


def prepare(config, app, current, repo=DEFAULT_REPO, progress=None):
    config = Path(config).resolve(strict=True)
    if config.stat().st_mode & 0o077: raise PermissionError('Runtime configuration must be private')
    app = validate_target(app)
    release, info = check(current, repo, progress)
    if not info['available']: raise ValueError('No newer stable release is available')
    stage = config.parent / 'updates' / (release['tag_name'] + '-' + uuid.uuid4().hex[:12])
    stage.mkdir(parents=True, mode=0o700); stage.parent.chmod(0o700)
    asset = release_asset(release, 'AIRemote-macos.zip', repo)
    archive = fetch_asset(asset, stage, progress=progress)
    extract_zip(archive, stage / 'unpacked', progress=progress)
    candidate = stage / 'unpacked/AIRemote.app'
    progress_event(progress, 'verifying_app')
    verify_bundle(candidate, release['tag_name'])
    installer = candidate / 'Contents/Resources/runtime/deploy/update_mac.py'
    if not installer.is_file(): raise ValueError('Update does not contain a bundled installer')
    plan = stage / 'plan.json'
    plan.write_text(json.dumps({'config': str(config), 'app': str(app), 'candidate': str(candidate),
        'version': release['tag_name'], 'sha256': asset['sha256'], 'installer': str(installer)}, indent=2))
    plan.chmod(0o600)
    progress_event(progress, 'prepared')
    return {**info, 'plan': str(plan)}


def load_plan(path):
    path = Path(path).resolve(strict=True)
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError('Update plan must be private and owned by this user')
    plan = json.loads(path.read_text()); config = Path(plan['config']).resolve(strict=True)
    if not path.is_relative_to(config.parent / 'updates'): raise ValueError('Update plan is outside the private staging directory')
    candidate = Path(plan['candidate']).resolve(strict=True)
    if not candidate.is_relative_to(path.parent / 'unpacked'): raise ValueError('Candidate is outside staging')
    validate_target(plan['app']); verify_bundle(candidate, plan['version'])
    return plan


def install_app(candidate, app, config, controller=start_services, opener=None):
    """Install only already-verified data. Preserve credentials and restore on failure."""
    candidate, app, config = Path(candidate), Path(app), Path(config)
    previous_runtime = runtime(app, config)
    controller(config, stop=True, runtime_root=previous_runtime)
    backup = app.with_name('.AIRemote-backup-' + uuid.uuid4().hex[:10] + '.app')
    temporary = app.with_name('.AIRemote-installing-' + uuid.uuid4().hex[:10] + '.app')
    moved = False
    try:
        shutil.copytree(candidate, temporary)
        if app.exists(): os.replace(app, backup); moved = True
        os.replace(temporary, app)
        controller(config, runtime_root=runtime(app, config))
    except BaseException:
        if moved:
            if app.exists(): shutil.rmtree(app)
            os.replace(backup, app)
        if temporary.exists(): shutil.rmtree(temporary)
        try: controller(config, runtime_root=runtime(app, config))
        finally:
            if opener: opener(app)
        raise
    if opener: opener(app)
    return {'installed': True, 'app': str(app), 'backup': str(backup) if moved else None}


def wait_for_exit(pid):
    if not pid: return
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try: os.kill(pid, 0)
        except ProcessLookupError: return
        time.sleep(.2)
    raise TimeoutError('AIRemote did not close; update was not installed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true'); mode.add_argument('--prepare', action='store_true')
    mode.add_argument('--launch-install', type=Path); mode.add_argument('--perform', type=Path)
    parser.add_argument('--config', type=Path); parser.add_argument('--app', type=Path, default=Path('/Applications/AIRemote.app'))
    parser.add_argument('--current-version'); parser.add_argument('--repo', default=DEFAULT_REPO)
    parser.add_argument('--wait-pid', type=int, default=0)
    parser.add_argument('--progress', action='store_true', help='Emit live NDJSON progress before the final JSON result')
    args = parser.parse_args()
    progress = (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if args.progress else None
    if args.perform:
        plan = load_plan(args.perform); wait_for_exit(args.wait_pid)
        lock_path = Path(plan['config']).parent / 'updates/install.lock'
        with lock_path.open('a') as lock:
            lock_path.chmod(0o600); fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = install_app(plan['candidate'], plan['app'], plan['config'],
                opener=lambda app: subprocess.run(['/usr/bin/open', '-n', str(app)], check=True))
    elif args.launch_install:
        plan = load_plan(args.launch_install)
        log = args.launch_install.parent / 'install.log'
        with log.open('ab') as output:
            log.chmod(0o600)
            process = subprocess.Popen([sys.executable, plan['installer'], '--perform', str(args.launch_install),
                '--wait-pid', str(args.wait_pid)], stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True)
        result = {'installerStarted': True, 'pid': process.pid, 'log': str(log)}
    else:
        current = args.current_version or app_info(args.app)['CFBundleShortVersionString']
        if args.check: _, result = check(current, args.repo, progress)
        else:
            if not args.config: parser.error('--prepare requires --config')
            result = prepare(args.config, args.app, current, args.repo, progress)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), flush=True); raise SystemExit(1)
