#!/usr/bin/env python3
"""Redeem a one-time pairing code and save private credentials on the Mac."""

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from configure_mac import configure, relay_origin

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'relay'))
from local_lock import exclusive_lock


class DirectOnly(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def claim(origin, body):
    request = Request(origin + '/api/pairing/claim', json.dumps(body).encode(),
                      {'Content-Type': 'application/json'}, method='POST')
    client = build_opener(DirectOnly)
    for attempt in range(3):
        try:
            with client.open(request, timeout=8) as response:
                return json.loads(response.read(8192))
        except HTTPError as error:
            if error.code < 500:
                try:
                    message = json.loads(error.read(8192)).get('error')
                except (ValueError, AttributeError):
                    message = None
                raise ValueError(message or f'服务器拒绝配对（{error.code}）。') from error
        except (OSError, URLError):
            pass
        if attempt < 2:
            time.sleep(.5 * (attempt + 1))
    raise RuntimeError('暂时无法确认配对，请在这台 Mac 使用同一个配对码重试。')


def pair(data, connections=None, request=claim):
    origin = relay_origin(data.get('origin'))
    code, name = data.get('code'), data.get('name')
    if not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,100}', code):
        raise ValueError('请输入从网页复制的完整配对码。')
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ValueError('设备名称使用 1–80 个字符。')
    workspace = data.get('workspace')
    if not isinstance(workspace, str) or not Path(workspace).is_absolute() or not Path(workspace).is_dir():
        raise ValueError('请选择要授权的项目文件夹。')
    root = Path(connections or Path.home() / 'Library/Application Support/AIRemote/Connections')
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    home = root / ('pair-' + hashlib.sha256((origin + '\n' + code).encode()).hexdigest()[:24])
    with exclusive_lock(root / 'pairing.lock'):
        config = home / '.local/mac-runtime.json'
        if config.is_file():
            agent = home / '.local/relay-agent.json'
            if os.name != 'nt' and (config.stat().st_mode & 0o077 or agent.stat().st_mode & 0o077):
                raise PermissionError('配对配置的访问权限无效。')
            saved = json.loads(agent.read_text())
            token = saved['agent_token']
            if saved['relay_url'] != origin:
                raise ValueError('配对配置的网站地址不一致。')
        else:
            home.mkdir(mode=0o700)
            token = secrets.token_urlsafe(32)
            try:
                configure(home, origin, token, [workspace], name=name.strip())
            except BaseException:
                # The server has not been contacted yet; only our new staging directory is removed.
                shutil.rmtree(home)
                raise
        result = request(origin, {'code': code, 'name': name.strip(), 'platform': 'windows' if os.name == 'nt' else 'macos', 'tokenHash': hashlib.sha256(token.encode()).hexdigest()})
        device_id = str(uuid.UUID(result['deviceId']))
    return {'paired': True, 'directory': str(home.resolve()), 'deviceId': device_id}


if __name__ == '__main__':
    try:
        print(json.dumps(pair(json.load(sys.stdin)), ensure_ascii=False))
    except Exception as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False))
        raise SystemExit(1)
