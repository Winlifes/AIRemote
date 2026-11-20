#!/usr/bin/env python3
"""Prepare an opt-in Codex CLI launcher; never quit or patch Codex."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'poc'))
from app_server_rpc import find_codex_installation
from goal_transport import DEFAULT_DIRECTORY, GoalOwnerClient, GoalUnavailable, private_path


def locate_codex():
    bundle, info, executable = find_codex_installation()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise FileNotFoundError('Codex bundled CLI not found')
    return bundle, info, executable


def prepare(directory=DEFAULT_DIRECTORY):
    directory = Path(directory).expanduser().resolve()
    # Keep AF_UNIX paths below macOS's 104-byte sun_path limit.
    if len(os.fsencode(str(directory / ('s' + '0' * 12 + '.sock')))) >= 104:
        raise ValueError('目标通道目录路径过长。')
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    private_path(directory, True)
    bundle, info, executable = locate_codex()
    config_path = directory / 'config.json'
    token = secrets.token_urlsafe(48)
    if config_path.exists():
        old = json.loads(private_path(config_path).read_text())
        token = old['token']
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError('Invalid existing goal channel token')
    module = (ROOT / 'poc/goal_transport.py').read_bytes()
    # Running proxies retain their code across AIRemote app replacement.
    target = directory / ('transport-' + hashlib.sha256(module).hexdigest()[:16] + '.py')
    if target.exists():
        private_path(target)
        if target.read_bytes() != module:
            raise ValueError('Goal transport integrity mismatch')
    else:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(module)
    temporary = directory / 'config.next'
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'token': token, 'executable': str(executable), 'bundleInfo': str(bundle / 'Contents/Info.plist'),
                   'codexVersion': info.get('CFBundleShortVersionString')}, stream)
    os.replace(temporary, config_path)
    launcher = directory / 'codex'
    content = '#!/bin/sh\nexec /usr/bin/python3 ' + shlex.quote(str(target)) + ' --config ' + shlex.quote(str(config_path)) + ' -- "$@"\n'
    next_launcher = directory / 'codex.next'
    fd = os.open(str(next_launcher), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    with os.fdopen(fd, 'w') as stream:
        stream.write(content)
    os.replace(next_launcher, launcher)
    return {'configured': True, 'launcher': str(launcher), 'bundle': str(bundle), 'codexVersion': info.get('CFBundleShortVersionString')}


def status(directory=DEFAULT_DIRECTORY):
    client = GoalOwnerClient(directory)
    try:
        records = client._records()
    except (GoalUnavailable, OSError, ValueError):
        return {'configured': False, 'connected': False}
    ready = 0
    for record in records:
        try:
            ready += int(client._call(record, 'status', {}, timeout=1).get('ready', False))
        except (RuntimeError, OSError, ValueError, KeyError):
            pass
    return {'configured': True, 'connected': bool(ready)}


def launch(directory=DEFAULT_DIRECTORY):
    result = prepare(directory)
    bundle, info, _ = locate_codex()
    executable = str(bundle / 'Contents/MacOS' / info['CFBundleExecutable'])
    processes = subprocess.run(['/bin/ps', '-axww', '-o', 'comm='], capture_output=True, text=True, check=True).stdout.splitlines()
    if executable in [value.strip() for value in processes]:
        result.update({'launched': False, 'needsRestart': True,
            'message': '通道已准备好。请先正常退出 Codex，再点击“通过连接通道启动 Codex”。'})
        return result
    subprocess.run(['/usr/bin/open', '--env', 'CODEX_CLI_PATH=' + result['launcher'], '-a', str(bundle)], check=True, capture_output=True)
    result.update({'launched': True, 'needsRestart': False, 'message': '已通过连接通道启动 Codex。可以从手机发送指令，继续已有对话。'})
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--prepare', action='store_true')
    action.add_argument('--status', action='store_true')
    action.add_argument('--launch', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(status() if args.status else launch() if args.launch else prepare(), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False)); raise SystemExit(1)
