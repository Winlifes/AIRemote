#!/usr/bin/env python3
"""Start and health-check AIRemote's detached Mac processes."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.request import Request, build_opener, ProxyHandler

from mac_background import BackgroundService


def alive(pid, script):
    if not isinstance(pid, int):
        return False
    command = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
    return str(script) in command


def connector_healthy(port, token):
    # Local credentials must never be sent through an inherited HTTP proxy.
    try:
        request = Request('http://127.0.0.1:%s/api/health' % port, headers={'Authorization': 'Bearer ' + token})
        with build_opener(ProxyHandler({})).open(request, timeout=2) as response:
            return response.status == 200 and json.load(response).get('ok') is True
    except (OSError, ValueError):
        return False


def automatic_update_lock(config_path):
    return Path(config_path).resolve().parent / 'mac-automatic-update.lock'


def automatic_update_readiness(config_path, runtime_root):
    config_path, cfg = read_runtime_config(config_path)
    pid_path = config_path.parent / 'mac-processes.json'
    recorded = json.loads(pid_path.read_text()) if pid_path.exists() else {}
    scripts = recorded_scripts(config_path, cfg, scripts_at(runtime_root))
    running = alive(recorded.get('connector'), scripts['connector'])
    agent_running = alive(recorded.get('agent'), scripts['agent'])
    if agent_running and not alive(recorded['agent'], '--update-lock ' + str(automatic_update_lock(config_path))):
        return False, True
    token = Path(cfg['token_file']).read_text().strip()
    port = int(cfg.get('connector_port', 8765))
    if not running:
        return not connector_healthy(port, token), agent_running
    try:
        request = Request('http://127.0.0.1:%s/api/update-readiness' % port,
                          headers={'Authorization': 'Bearer ' + token})
        with build_opener(ProxyHandler({})).open(request, timeout=4) as response:
            status = json.load(response)
            return isinstance(status, dict) and status.get('idle') is True, True
    except (OSError, ValueError):
        return False, True


def stop_processes(processes, scripts, names):
    for name in names:
        pid = processes.get(name)
        if alive(pid, scripts[name]):
            try:
                os.kill(pid, signal.SIGINT)
            except ProcessLookupError:
                pass
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and any(alive(processes.get(n), scripts[n]) for n in names):
        time.sleep(.2)
    if any(alive(processes.get(n), scripts[n]) for n in names):
        raise RuntimeError('AIRemote 仍在结束请求，请稍后再试；没有强制终止正在执行的任务。')
    for name in names:
        processes.pop(name, None)


def save_processes(path, processes):
    temporary = path.with_suffix('.next')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(processes, output)
    os.replace(temporary, path)


def scripts_at(root):
    root = Path(root).resolve(strict=True)
    return {'connector': root / 'poc/control_server.py', 'agent': root / 'relay/agent.py'}


def recorded_scripts(config_path, cfg, fallback):
    path = Path(config_path).parent / 'mac-service-scripts.json'
    if not path.exists(): return fallback
    if path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077:
        raise PermissionError('Service script record must be private and belong to this user')
    record = json.loads(path.read_text())
    if record.get('config') != str(Path(config_path).resolve()): raise ValueError('Service record belongs to a different configuration')
    scripts = {key: Path(record['scripts'][key]) for key in ['connector', 'agent']}
    if not str(scripts['connector']).endswith('/poc/control_server.py') or not str(scripts['agent']).endswith('/relay/agent.py'):
        raise ValueError('Invalid AIRemote service script record')
    return scripts


class ConfigUnavailable(RuntimeError):
    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code


def read_runtime_config(location):
    try:
        path = Path(location).resolve(strict=True)
        if path.stat().st_mode & 0o077:
            raise PermissionError('启动配置文件需要设为仅本人可读写（0600）。')
        return path, json.loads(path.read_text())
    except FileNotFoundError as error:
        raise ConfigUnavailable(os.EX_NOINPUT, f'未找到启动配置文件\n{location}') from error
    except PermissionError as error:
        raise ConfigUnavailable(os.EX_NOPERM, str(error)) from error


def start_services(config_path, restart=False, stop=False, startup_timeout=20, runtime_root=None, keep_relay=False):
    config_path, cfg = read_runtime_config(config_path)
    project = Path(cfg['project']).resolve(strict=True)
    private = config_path.parent
    logs = private / 'logs'; logs.mkdir(mode=0o700, exist_ok=True)
    pid_path = private / 'mac-processes.json'
    scripts = scripts_at(runtime_root or cfg.get('runtime_root') or project)
    port = int(cfg.get('connector_port', 8765))
    token = Path(cfg['token_file']).read_text().strip()
    # Hold this lock through health checks so simultaneous double-clicks reuse
    # one instance. __exit__ releases it on every failure path as well.
    with open(private / 'mac-start.lock', 'a+') as lock:
        os.chmod(lock.name, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        existing = recorded_scripts(config_path, cfg, scripts_at(cfg.get('runtime_root') or project))
        processes = json.loads(pid_path.read_text()) if pid_path.exists() else {}
        if existing != scripts and any(alive(processes.get(name), existing[name]) for name in existing):
            if restart or stop:
                stop_processes(processes, existing, ['agent', 'connector']); save_processes(pid_path, processes)
            else:
                # A source checkout can inspect/reuse a newer bundled runtime;
                # it must not accidentally replace it with an older checkout.
                scripts = existing
        result = launch_locked(cfg, project, scripts, logs, pid_path, port, token, restart, stop, startup_timeout, keep_relay)
        record = private / 'mac-service-scripts.json'
        if stop: record.unlink(missing_ok=True)
        else:
            save_processes(record, {'config': str(config_path), 'scripts': {key: str(value) for key, value in scripts.items()}})
        return result


def service_status(config_path, runtime_root=None):
    config_path, cfg = read_runtime_config(config_path)
    project = Path(cfg['project']).resolve(strict=True)
    pid_path = config_path.parent / 'mac-processes.json'
    recorded = json.loads(pid_path.read_text()) if pid_path.exists() else {}
    scripts = recorded_scripts(config_path, cfg, scripts_at(runtime_root or cfg.get('runtime_root') or project))
    running = {name: pid for name, pid in recorded.items() if name in scripts and alive(pid, scripts[name])}
    token = Path(cfg['token_file']).read_text().strip()
    port = int(cfg.get('connector_port', 8765))
    healthy = 'connector' in running and connector_healthy(port, token)
    providers = []
    if healthy:
        try:
            request = Request('http://127.0.0.1:%s/api/providers' % port, headers={'Authorization': 'Bearer ' + token})
            with build_opener(ProxyHandler({})).open(request, timeout=4) as response:
                providers = json.load(response).get('providers', [])
        except (OSError, ValueError):
            pass
    background = BackgroundService() if sys.platform == 'darwin' else None
    registered = bool(background and background.configuration() == config_path)
    managed = registered and background.ready()
    return {'running': running, 'connectorHealthy': healthy, 'agentRunning': 'agent' in running,
            'backgroundRegistered': registered, 'backgroundManaged': managed, 'relay': cfg['relay_url'],
            'providers': providers, 'logsDirectory': str(config_path.parent / 'logs')}


def launch_locked(cfg, project, scripts, logs, pid_path, port, token, restart, stop, startup_timeout, keep_relay=False):
    processes = json.loads(pid_path.read_text()) if pid_path.exists() else {}
    if stop or restart:
        stop_processes(processes, scripts, ['agent', 'connector'])
        save_processes(pid_path, processes)
        if stop:
            pid_path.unlink(missing_ok=True)
            return {'stopped': True}
    recovered = []
    connector = [sys.executable, str(scripts['connector'])]
    for flag, values in [('--thread', cfg['threads']), ('--artifact-root', cfg['artifact_roots']), ('--workspace-root', cfg['workspace_roots'])]:
        for value in values: connector.extend([flag, value])
    connector.extend(['--state-dir', cfg['connector_state'], '--token-file', cfg['token_file'], '--port', str(port)])
    if cfg.get('discover_desktop_projects'):
        connector.append('--discover-desktop-projects')
    commands = {'connector': connector, 'agent': [sys.executable, str(scripts['agent']), '--config', cfg['agent_config'],
                                                '--update-lock', str(pid_path.parent / 'mac-automatic-update.lock')]}
    names = ('agent', 'connector') if keep_relay else ('connector', 'agent')
    started = []
    launched = {}
    def running(name):
        # Our child handle is authoritative during exec. On a busy Mac, `ps`
        # can briefly show the Python executable without its script arguments.
        child = launched.get(name)
        return child.poll() is None if child is not None else alive(processes.get(name), scripts[name])
    for name in names:
        if name == 'connector' and alive(processes.get(name), scripts[name]) and not connector_healthy(port, token):
            print('检测到失效的 Codex 连接器，正在恢复…', flush=True)
            stop_processes(processes, scripts, ['connector'])
            save_processes(pid_path, processes)
            recovered.append('connector')
        if not alive(processes.get(name), scripts[name]):
            if name == 'connector' and connector_healthy(port, token):
                raise RuntimeError('端口已有未登记的连接器，请核对进程；本次没有终止其他进程。')
            with (logs / (name + '.log')).open('ab') as output:
                os.chmod(output.name, 0o600)
                child = subprocess.Popen(commands[name], cwd=project, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                         start_new_session=True, close_fds=True, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
            processes[name] = child.pid
            launched[name] = child
            started.append(name)
            save_processes(pid_path, processes)
        if name == 'connector':
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if not running(name):
                    raise RuntimeError('Codex 连接器启动后退出，请查看 ' + str(logs / 'connector.log'))
                if connector_healthy(port, token):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError('Codex 连接器未通过健康检查，请查看 ' + str(logs / 'connector.log'))
        elif name in started:
            # A duplicate-agent lock or missing config exits immediately.
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                if not running(name):
                    raise RuntimeError('公网连接进程启动后退出，请查看 ' + str(logs / 'agent.log'))
                time.sleep(.1)
    if not connector_healthy(port, token):
        raise RuntimeError('Codex 连接器已失效，请查看 ' + str(logs / 'connector.log'))
    return {'running': processes, 'relay': cfg['relay_url'], 'terminalMayClose': True,
            'started': started, 'recovered': recovered}


def control_services(config_path, restart=False, stop=False, runtime_root=None, managed=False):
    if sys.platform != 'darwin':
        return start_services(config_path, restart=restart, stop=stop, runtime_root=runtime_root)
    config, cfg = read_runtime_config(config_path)
    runtime = Path(runtime_root or cfg.get('runtime_root') or cfg['project']).resolve(strict=True)
    background = BackgroundService()
    registered = background.configuration() == config
    if not registered and not managed:
        return start_services(config, restart=restart, stop=stop, runtime_root=runtime)
    with background.locked():
        registered = background.configuration() == config
        if registered and (stop or restart):
            background.disable()
        if stop:
            return start_services(config, stop=True, runtime_root=runtime)
        if restart:
            start_services(config, stop=True, runtime_root=runtime)
        warning = None
        try:
            background.enable(config, runtime)
        except (OSError, subprocess.SubprocessError, RuntimeError) as error:
            warning = str(error)
        result = start_services(config, runtime_root=runtime, keep_relay=True)
        return {**result, 'backgroundManaged': background.ready() if warning is None else False, 'backgroundWarning': warning}


def supervise(config, runtime, pid):
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    background = BackgroundService()
    ready = False
    delay = 3
    while not stopped.is_set():
        try:
            result = start_services(config, runtime_root=runtime, keep_relay=True)
            if not ready and not stopped.is_set():
                background.mark_ready(pid)
                ready = True
            if result['started']:
                print('已恢复 AIRemote 进程 ' + ', '.join(result['started']), flush=True)
            delay = 4
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            if ready and isinstance(error, (ConfigUnavailable, PermissionError)):
                background.ready_file.unlink(missing_ok=True)
                ready = False
            print(str(error), flush=True)
            delay = min(delay * 2, 30)
        stopped.wait(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--managed', action='store_true')
    parser.add_argument('--supervise', action='store_true')
    parser.add_argument('--supervisor-pid', type=int, help=argparse.SUPPRESS)
    parser.add_argument('--status', action='store_true', help='Read status without starting or stopping anything')
    parser.add_argument('--runtime-root', help='Code bundled with the application; configuration stays outside it')
    args = parser.parse_args()
    if args.supervise:
        return supervise(args.config, args.runtime_root, args.supervisor_pid or os.getpid())
    result = service_status(args.config, args.runtime_root) if args.status else control_services(
        args.config, restart=args.restart, stop=args.stop, runtime_root=args.runtime_root, managed=args.managed)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('AIRemote 启动失败：' + str(error), file=sys.stderr, flush=True)
        sys.exit(error.exit_code if isinstance(error, ConfigUnavailable) else 1)
