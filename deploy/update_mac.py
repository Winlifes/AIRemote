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

from start_mac import automatic_update_lock, automatic_update_readiness, start_services
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
    installer = candidate / 'Contents/Resources/runtime/deploy/update_mac.py'
    if Path(plan['installer']).resolve(strict=True) != installer or not installer.is_file():
        raise ValueError('The staged installer does not match the verified app')
    return plan


def install_app(candidate, app, config, controller=start_services, opener=None, restart_services=True):
    """Install only already-verified data. Preserve credentials and restore on failure."""
    candidate, app, config = Path(candidate), Path(app), Path(config)
    backup = app.with_name('.AIRemote-backup-' + uuid.uuid4().hex[:10] + '.app')
    temporary = app.with_name('.AIRemote-installing-' + uuid.uuid4().hex[:10] + '.app')
    moved = replaced = stopped = False
    try:
        if restart_services:
            controller(config, stop=True, runtime_root=runtime(app, config))
            stopped = True
        shutil.copytree(candidate, temporary)
        if app.exists(): os.replace(app, backup); moved = True
        os.replace(temporary, app)
        replaced = True
        if restart_services:
            # A launcher opened during the swap can have spawned the previous services.
            controller(config, restart=True, runtime_root=runtime(app, config))
    except BaseException:
        if replaced:
            shutil.rmtree(app)
        if moved:
            os.replace(backup, app)
        if temporary.exists(): shutil.rmtree(temporary)
        if stopped:
            controller(config, restart=True, runtime_root=runtime(app, config))
        raise
    finally:
        if opener and app.exists(): opener(app)
    return {'installed': True, 'app': str(app), 'backup': str(backup) if moved else None}


def launch_install(plan_path, wait_pid, automatic=False, in_menu_bar=False, idle_timeout=24):
    plan_path = Path(plan_path).resolve(strict=True)
    plan = load_plan(plan_path)
    arguments = [sys.executable, plan['installer'], '--perform', str(plan_path), '--wait-pid', str(wait_pid)]
    if not automatic:
        return detached_installer(plan_path, arguments)
    if not os.access(Path(plan['app']).parent, os.W_OK):
        raise PermissionError('AIRemote cannot update this Applications folder without permission')

    # The Mac agent holds a shared lock through each lease and its result upload.
    # Pass this descriptor to the installer so the pause survives the GUI exit.
    lock_path = automatic_update_lock(plan['config'])
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+b') as pause:
        deadline = time.monotonic() + idle_timeout
        while True:
            try:
                fcntl.flock(pause, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return {'deferred': True}
                time.sleep(.025)
        idle, running = automatic_update_readiness(plan['config'], runtime(plan['app'], plan['config']))
        if not idle:
            return {'deferred': True}
        arguments.append('--background')
        if in_menu_bar:
            arguments.append('--in-menu-bar')
        if not running:
            arguments.append('--keep-services-stopped')
        return detached_installer(plan_path, arguments, (pause.fileno(),))


def detached_installer(plan_path, arguments, pass_fds=()):
    log = plan_path.parent / 'install.log'
    with log.open('ab') as output:
        log.chmod(0o600)
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, close_fds=True, pass_fds=pass_fds)
    return {'installerStarted': True, 'pid': process.pid, 'log': str(log)}


def reopen_app(app, background, in_menu_bar, keep_services_stopped):
    args = ['/usr/bin/open', '-n']
    if background:
        args.append('-g')
    app_args = [flag for flag, enabled in (('--in-menu-bar', in_menu_bar),
                ('--keep-services-stopped', keep_services_stopped)) if enabled]
    subprocess.run([*args, str(app), *(['--args', *app_args] if app_args else [])], check=True)


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
    parser.add_argument('--automatic', action='store_true')
    parser.add_argument('--background', action='store_true')
    parser.add_argument('--in-menu-bar', action='store_true')
    parser.add_argument('--keep-services-stopped', action='store_true')
    parser.add_argument('--progress', action='store_true', help='Emit live NDJSON progress before the final JSON result')
    args = parser.parse_args()
    progress = (lambda event: print(json.dumps(event, ensure_ascii=False), flush=True)) if args.progress else None
    if args.perform:
        plan = load_plan(args.perform); wait_for_exit(args.wait_pid)
        lock_path = Path(plan['config']).parent / 'updates/install.lock'
        app = Path(plan['app'])
        try:
            with lock_path.open('a') as lock:
                lock_path.chmod(0o600); fcntl.flock(lock, fcntl.LOCK_EX)
                if app.exists() and version_tuple(app_info(app)['CFBundleShortVersionString']) >= version_tuple(plan['version']):
                    result = {'alreadyCurrent': True}
                else:
                    result = install_app(plan['candidate'], app, plan['config'], restart_services=not args.keep_services_stopped)
        finally:
            if app.exists(): reopen_app(app, args.background, args.in_menu_bar, args.keep_services_stopped)
    elif args.launch_install:
        result = launch_install(args.launch_install, args.wait_pid, args.automatic, args.in_menu_bar)
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
