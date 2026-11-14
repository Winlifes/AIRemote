"""Small authenticated client used by the live, public relay verification."""
import json
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RelayClient:
    def __init__(self, origin, token):
        self.origin = origin.rstrip('/')
        self.token = token

    def http(self, path, body=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + self.token
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        try:
            response = urlopen(Request(self.origin + path, data=data, headers=headers), timeout=35)
        except HTTPError as error:
            response = error
        with response:
            return response.code, dict(response.headers), response.read()

    def api(self, path, body=None):
        status, _, content = self.http(path, body)
        result = json.loads(content)
        if status >= 400:
            raise RuntimeError(str(status) + ': ' + str(result))
        return result

    def bridge_raw(self, method, path, body=None, key=None):
        key = key or str(uuid.uuid4())
        job = self.api('/api/requests', {'method': method, 'path': path, 'body': body or {}, 'key': key})
        deadline = time.monotonic() + 145
        while time.monotonic() < deadline:
            state = self.api('/api/requests/' + job['id'])
            if state['state'] == 'done':
                status, headers, content = self.http('/api/requests/' + job['id'] + '/result')
                return status, headers, content, state
            if state['state'] in {'unknown', 'expired', 'result_expired'}:
                raise RuntimeError('Relay request ended in state ' + state['state'])
            time.sleep(.25)
        raise TimeoutError('Relay request still in progress: ' + job['id'])

    def bridge(self, method, path, body=None, key=None):
        status, _, content, _ = self.bridge_raw(method, path, body, key)
        value = json.loads(content)
        if status >= 400:
            raise RuntimeError(str(status) + ': ' + str(value))
        return value
