"""Windows AIRemote launcher for the local Codex reader and outbound agent."""

import csv
import json
import os
from pathlib import Path
from queue import Empty, Queue
import socket
import stat
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
for directory in ('relay', 'deploy', 'poc'):
    sys.path.insert(0, str(ROOT / directory))

from agent import Agent
from connector import CodexWindows, Connector
from local_lock import exclusive_lock
from pair_mac import pair


def private_directory():
    base = Path(os.environ['LOCALAPPDATA']).resolve(strict=True)
    root = base / 'AIRemote'
    root.mkdir(exist_ok=True)
    if not root.resolve().is_relative_to(base) or root.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise PermissionError('AIRemote 配置目录必须是本地真实文件夹。')
    user = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'], text=True, timeout=10)
    sid = next((row[-1] for row in csv.reader(user.splitlines()) if row and row[-1].startswith('S-1-')), None)
    if not sid:
        raise PermissionError('无法识别当前 Windows 用户。')
    subprocess.run(['icacls.exe', str(root), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                   check=True, timeout=10, capture_output=True)
    return root


class Launcher:
    def __init__(self, window, private):
        self.window, self.private = window, private
        self.origin = tk.StringVar()
        self.code = tk.StringVar()
        self.name = tk.StringVar(value=socket.gethostname())
        self.project = tk.StringVar()
        self.status = tk.StringVar(value='请配对电脑')
        self.events = Queue()
        self.agent = self.connector = None
        self.running = False
        self.busy = False

        window.title('AIRemote')
        window.minsize(460, 320)
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text='AIRemote', font=('Segoe UI', 17, 'bold')).grid(row=0, columnspan=2, sticky='w')
        ttk.Label(frame, textvariable=self.status, wraplength=420).grid(row=1, columnspan=2, sticky='we', pady=(4, 16))
        self.entries = []
        for row, label, variable in ((2, '网站地址', self.origin), (3, '配对码', self.code), (4, '设备名称', self.name), (5, '项目文件夹', self.project)):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=5, padx=(0, 10))
            entry = ttk.Entry(frame, textvariable=variable, show='●' if row == 3 else '')
            entry.grid(row=row, column=1, sticky='we', pady=5)
            self.entries.append(entry)
        self.folder_button = ttk.Button(frame, text='选择项目…', command=self.choose)
        self.folder_button.grid(row=6, column=1, sticky='e')
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, columnspan=2, sticky='w', pady=(16, 0))
        self.attach = ttk.Button(buttons, text='配对并启动', command=self.attach_clicked)
        self.attach.pack(side='left', padx=(0, 8))
        self.stop_button = ttk.Button(buttons, text='停止服务', command=self.stop_clicked, state='disabled')
        self.stop_button.pack(side='left', padx=(0, 8))
        ttk.Button(buttons, text='打开网页', command=self.open_site).pack(side='left')
        ttk.Label(frame, text='Windows Codex · 读取已授权项目的会话和文件').grid(row=8, columnspan=2, sticky='w', pady=(12, 0))
        window.protocol('WM_DELETE_WINDOW', self.close)
        window.after(100, self.poll)
        saved = self.saved_directory()
        if saved:
            cfg = json.loads((saved / '.local/mac-runtime.json').read_text())
            self.origin.set(cfg['relay_url'])
            self.project.set(cfg['workspace_roots'][0])
            self.work(lambda: self.start(saved), '正在启动服务…')

    def saved_directory(self):
        try:
            home = Path((self.private / 'connection.txt').read_text()).resolve(strict=True)
            if home.is_relative_to(self.private / 'Connections') and (home / '.local/mac-runtime.json').is_file():
                return home
        except OSError:
            pass
        return None

    def choose(self):
        folder = filedialog.askdirectory(title='授权 Windows Codex 项目目录')
        if folder:
            self.project.set(folder)

    def open_site(self):
        url = self.origin.get().strip()
        if url.startswith('https://'):
            webbrowser.open(url.rstrip('/') + '/account')

    def attach_clicked(self):
        if self.busy:
            return
        values = {'origin': self.origin.get(), 'code': self.code.get().strip(),
                  'name': self.name.get(), 'workspace': self.project.get()}
        saved = self.saved_directory()
        if not values['code'] and saved:
            self.work(lambda: self.start(saved), '正在启动服务…')
        else:
            self.work(lambda: self.enroll(values), '正在配对…')

    def enroll(self, values):
        result = pair(values, self.private / 'Connections')
        path = Path(result['directory'])
        self.start(path)
        stage = self.private / 'connection.next'
        stage.write_text(str(path))
        os.replace(stage, self.private / 'connection.txt')

    def start(self, home):
        self.stop()
        cfg = json.loads((home / '.local/mac-runtime.json').read_text())
        agent_cfg = json.loads(Path(cfg['agent_config']).read_text())
        token = Path(cfg['token_file']).read_text().strip()
        connector = Connector(0, token, CodexWindows(cfg['workspace_roots']))
        try:
            agent = Agent({**agent_cfg, 'connector_url': f'http://127.0.0.1:{connector.server_port}'})
        except BaseException:
            connector.server_close()
            raise
        self.connector, self.agent = connector, agent
        threading.Thread(target=connector.serve_forever, daemon=True).start()
        self.agent_thread = threading.Thread(target=agent.run, daemon=True)
        self.agent_thread.start()
        self.running = True

    def stop(self):
        if self.agent:
            self.agent.stop.set()
            self.agent_thread.join(timeout=36)
        if self.connector:
            self.connector.shutdown()
            self.connector.server_close()
        self.connector = self.agent = None
        self.running = False

    def work(self, action, message):
        self.busy = True
        self.status.set(message)
        self.activate_controls()
        def task():
            try:
                action()
                self.events.put(None)
            except Exception as error:
                self.events.put(str(error))
        threading.Thread(target=task, daemon=True).start()

    def stop_clicked(self):
        if not self.busy:
            self.work(self.stop, '正在停止服务…')

    def activate_controls(self):
        for widget in [*self.entries, self.folder_button, self.attach]:
            widget.configure(state='disabled' if self.busy else 'normal')
        self.stop_button.configure(state='normal' if self.running and not self.busy else 'disabled')

    def poll(self):
        try:
            error = self.events.get_nowait()
            self.busy = False
            if error:
                self.status.set(error)
            else:
                self.code.set('')
                self.status.set('连接服务已启动' if self.running else '服务已停止')
            self.activate_controls()
        except Empty:
            if self.running and not self.busy and not self.agent_thread.is_alive():
                self.work(self.stop, '设备连接已失效，请重新配对。')
        self.window.after(180, self.poll)

    def close(self):
        if self.busy:
            return
        if self.agent:
            self.agent.stop.set()
        self.window.destroy()


def main():
    window = tk.Tk()
    try:
        private = private_directory()
        with exclusive_lock(private / 'launcher.lock'):
            Launcher(window, private)
            window.mainloop()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        messagebox.showerror('AIRemote 无法启动', str(error), parent=window)
        window.destroy()


if __name__ == '__main__':
    main()
