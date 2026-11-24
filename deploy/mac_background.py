"""Install the per-user launchd job that supervises the AIRemote connector."""

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import tempfile


LABEL = 'fun.winlife.airemote.connector'


class BackgroundService:
    def __init__(self, home=None, runner=subprocess.run):
        library = Path(home or Path.home()) / 'Library'
        self.directory = library / 'LaunchAgents'
        self.plist = self.directory / (LABEL + '.plist')
        self.logs = library / 'Logs/AIRemote'
        self.ready_file = self.logs / ('.' + LABEL + '.ready')
        self.domain = f'gui/{os.getuid()}'
        self.target = f'{self.domain}/{LABEL}'
        self.runner = runner

    def configuration(self):
        if not self.plist.exists():
            return None
        if self.plist.is_symlink():
            raise PermissionError('AIRemote 后台服务配置是符号链接')
        info = plistlib.loads(self.plist.read_bytes())
        args = info.get('ProgramArguments', [])
        if info.get('Label') != LABEL or not isinstance(args, list):
            raise ValueError('无法识别 AIRemote 后台服务配置')
        if len(args) == 4 and args[1:3] == ['--background-supervisor', '--config']:
            return Path(args[3])
        if len(args) == 7 and args[2:4] == ['--supervise', '--config']:
            return Path(args[4])
        raise ValueError('无法识别 AIRemote 后台服务配置')

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / ('.' + LABEL + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'r+b') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def command(self, *args):
        return self.runner(['/bin/launchctl', *args], capture_output=True, text=True, timeout=20)

    def loaded(self):
        return self.command('print', self.target).returncode == 0

    def ready(self):
        result = self.command('print', self.target)
        pid = re.search(r'^\tpid = (\d+)$', result.stdout, re.MULTILINE) if result.returncode == 0 else None
        try:
            return bool(pid and self.ready_file.read_text().strip() == pid[1])
        except OSError:
            return False

    def mark_ready(self, pid):
        with tempfile.NamedTemporaryFile(dir=self.logs, delete=False) as output:
            output.write(str(pid).encode('ascii'))
            temporary = Path(output.name)
        try:
            os.replace(temporary, self.ready_file)
        finally:
            temporary.unlink(missing_ok=True)

    def enable(self, config, runtime):
        existing = self.configuration()
        if existing and existing != config:
            raise RuntimeError('请先停止使用另一配置的 AIRemote 后台服务。')
        # launchd opens these before the app starts and has no access to Documents.
        self.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
        log = self.logs / 'supervisor.log'
        fd = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'ab'):
            os.fchmod(fd, 0o600)
        args = [sys.executable, str(runtime / 'deploy/start_mac.py'), '--supervise', '--config', str(config), '--runtime-root', str(runtime)]
        if runtime.parts[-3:] == ('Contents', 'Resources', 'runtime'):
            contents = runtime.parents[1]
            executable, bundle = contents / 'MacOS/AIRemote', contents / 'Info.plist'
            # The marker excludes older app bundles during an update rollback.
            if executable.is_file() and bundle.is_file() and plistlib.loads(bundle.read_bytes()).get('AIRemoteBackgroundSupervisor') is True:
                # Keep the application as the responsible process for its filesystem permissions.
                args = [str(executable), '--background-supervisor', '--config', str(config)]
        data = {'Label': LABEL, 'ProgramArguments': args, 'RunAtLoad': True, 'KeepAlive': True,
                'ThrottleInterval': 10, 'LimitLoadToSessionType': 'Aqua', 'AssociatedBundleIdentifiers': ['fun.winlife.airemote'],
                'StandardOutPath': str(log), 'StandardErrorPath': str(log)}
        encoded = plistlib.dumps(data)
        previous = self.plist.read_bytes() if existing else None
        if encoded == previous and self.loaded():
            return
        if self.loaded():
            self.unload()
        self.ready_file.unlink(missing_ok=True)
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as output:
            output.write(encoded)
            temporary = Path(output.name)
        try:
            os.replace(temporary, self.plist)
        finally:
            temporary.unlink(missing_ok=True)
        result = self.command('bootstrap', self.domain, str(self.plist))
        if result.returncode and not self.loaded():
            # Leave the plist for diagnosis and the next login. The caller can still launch manually.
            raise RuntimeError('macOS 未能注册 AIRemote 后台服务，请检查系统设置中的登录项。')

    def unload(self):
        if self.loaded():
            result = self.command('bootout', self.target)
            if result.returncode and self.loaded():
                raise RuntimeError('macOS 未能停止 AIRemote 后台服务。')

    def disable(self):
        self.unload()
        self.plist.unlink(missing_ok=True)
        self.ready_file.unlink(missing_ok=True)
