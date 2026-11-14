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
import time
from urllib.request import Request, build_opener, ProxyHandler


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


def start_services(config_path, restart=False, stop=False, startup_timeout=20):
    config_path = Path(config_path).resolve(strict=True)
    if config_path.stat().st_mode & 0o077:
        raise PermissionError('Runtime config must have mode 0600')
    cfg = json.loads(config_path.read_text())
    project = Path(cfg['project']).resolve(strict=True)
    private = config_path.parent
    logs = private / 'logs'; logs.mkdir(mode=0o700, exist_ok=True)
    pid_path = private / 'mac-processes.json'
    scripts = {'connector': project / 'poc/control_server.py', 'agent': project / 'relay/agent.py'}
    port = int(cfg.get('connector_port', 8765))
    token = Path(cfg['token_file']).read_text().strip()
    # Hold this lock through health checks so simultaneous double-clicks reuse
    # one instance. __exit__ releases it on every failure path as well.
    with open(private / 'mac-start.lock', 'a+') as lock:
        os.chmod(lock.name, 0o600); fcntl.flock(lock, fcntl.LOCK_EX)
        return launch_locked(cfg, project, scripts, logs, pid_path, port, token, restart, stop, startup_timeout)


def service_status(config_path):
    config_path = Path(config_path).resolve(strict=True)
    if config_path.stat().st_mode & 0o077:
        raise PermissionError('Runtime config must have mode 0600')
    cfg = json.loads(config_path.read_text())
    project = Path(cfg['project']).resolve(strict=True)
    pid_path = config_path.parent / 'mac-processes.json'
    recorded = json.loads(pid_path.read_text()) if pid_path.exists() else {}
    scripts = {'connector': project / 'poc/control_server.py', 'agent': project / 'relay/agent.py'}
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
    return {'running': running, 'connectorHealthy': healthy, 'agentRunning': 'agent' in running,
            'relay': cfg['relay_url'], 'providers': providers, 'logsDirectory': str(config_path.parent / 'logs')}


def launch_locked(cfg, project, scripts, logs, pid_path, port, token, restart, stop, startup_timeout):
    processes = json.loads(pid_path.read_text()) if pid_path.exists() else {}
    if stop or restart:
        stop_processes(processes, scripts, ['agent', 'connector'])
        save_processes(pid_path, processes)
        if stop:
            pid_path.unlink(missing_ok=True)
            return {'stopped': True}
    recovered = []
    if alive(processes.get('connector'), scripts['connector']) and not connector_healthy(port, token):
        print('检测到失效的 Codex 连接器，正在恢复…', flush=True)
        stop_processes(processes, scripts, ['connector'])
        save_processes(pid_path, processes)
        recovered.append('connector')
    connector = [sys.executable, str(scripts['connector'])]
    for flag, values in [('--thread', cfg['threads']), ('--artifact-root', cfg['artifact_roots']), ('--workspace-root', cfg['workspace_roots'])]:
        for value in values: connector.extend([flag, value])
    connector.extend(['--state-dir', cfg['connector_state'], '--token-file', cfg['token_file'], '--port', str(port)])
    if cfg.get('discover_desktop_projects'):
        connector.append('--discover-desktop-projects')
    commands = {'connector': connector, 'agent': [sys.executable, str(scripts['agent']), '--config', cfg['agent_config']]}
    started = []
    for name, command in commands.items():
        if not alive(processes.get(name), scripts[name]):
            if name == 'connector' and connector_healthy(port, token):
                raise RuntimeError('端口已有未登记的连接器，请核对进程；本次没有终止其他进程。')
            with (logs / (name + '.log')).open('ab') as output:
                os.chmod(output.name, 0o600)
                child = subprocess.Popen(command, cwd=project, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                         start_new_session=True, close_fds=True, env={**os.environ, 'PYTHONUNBUFFERED': '1'})
            processes[name] = child.pid
            started.append(name)
            save_processes(pid_path, processes)
        if name == 'connector':
            deadline = time.monotonic() + startup_timeout
            while time.monotonic() < deadline:
                if not alive(processes.get(name), scripts[name]):
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
                if not alive(processes.get(name), scripts[name]):
                    raise RuntimeError('公网连接进程启动后退出，请查看 ' + str(logs / 'agent.log'))
                time.sleep(.1)
    if not connector_healthy(port, token):
        raise RuntimeError('Codex 连接器已失效，请查看 ' + str(logs / 'connector.log'))
    return {'running': processes, 'relay': cfg['relay_url'], 'terminalMayClose': True,
            'started': started, 'recovered': recovered}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--status', action='store_true', help='Read status without starting or stopping anything')
    args = parser.parse_args()
    result = service_status(args.config) if args.status else start_services(args.config, restart=args.restart, stop=args.stop)
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('AIRemote 启动失败：' + str(error), file=sys.stderr, flush=True)
        sys.exit(1)
