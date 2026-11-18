"""The relay can invoke this fixed API surface, never arbitrary local URLs."""
import re
from urllib.parse import urlsplit, parse_qs

UUID = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
THREAD = r"/api/threads/" + UUID
DS = r'/api/providers/deepseek'
DS_THREAD = DS + r'/threads/session-' + UUID
CLAUDE = r'/api/providers/claude'
CLAUDE_THREAD = CLAUDE + r'/threads/' + UUID
MAX_BODY = 256 * 1024
MAX_RESULT = 64 * 1024 * 1024


def validate_request(value):
    if not isinstance(value, dict):
        raise ValueError("Request must be an object")
    method, path = value.get("method", "GET"), value.get("path", "")
    if not isinstance(path, str) or len(path) > 400 or not path.startswith("/api/"):
        raise ValueError("Invalid connector path")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or "%" in path or "\\" in path:
        raise ValueError("Only fixed local connector paths are allowed")
    routes = {
        "GET": [r'/api/providers', DS + r'/projects', DS + r'/projects/' + UUID + r'/threads', DS_THREAD, DS_THREAD + r'/(messages|queue|artifacts)', DS_THREAD + r'/artifacts/[0-9a-f]{24}/download', DS + r'/operations/' + UUID,
                CLAUDE + r'/projects', CLAUDE + r'/projects/' + UUID + r'/threads',
                CLAUDE_THREAD, CLAUDE_THREAD + r'/(messages|artifacts)', CLAUDE_THREAD + r'/artifacts/[0-9a-f]{24}/download', CLAUDE + r'/operations/' + UUID,
                r"/api/capabilities", r"/api/projects", r"/api/projects/" + UUID + r"/threads", r"/api/threads", THREAD, THREAD + r"/(messages|queue|artifacts|goal)",
                THREAD + r"/artifacts/[0-9a-f]{24}/download", r"/api/operations/" + UUID],
        "POST": [DS + r'/threads', DS_THREAD + r'/(messages|queue)', DS_THREAD + r'/queue/(?:msg-)?' + UUID + r'/send',
                 CLAUDE + r'/threads', CLAUDE_THREAD + r'/messages',
                 r"/api/threads", THREAD + r"/(messages|queue|goal)", THREAD + r"/queue/" + UUID + r"/send"],
        "PATCH": [DS_THREAD + r'/queue/(?:msg-)?' + UUID, THREAD + r"/queue/" + UUID, THREAD + r'/goal'],
        "PUT": [THREAD + r"/queue/order"],
        "DELETE": [DS_THREAD + r'/queue', DS_THREAD + r'/queue/(?:msg-)?' + UUID, THREAD + r"/queue", THREAD + r"/queue/" + UUID, THREAD + r'/goal'],
    }
    if not any(re.fullmatch(pattern, parsed.path) for pattern in routes.get(method, [])):
        raise ValueError("Connector operation is not allowed")
    if parsed.query:
        query = parse_qs(parsed.query, strict_parsing=True)
        if method != "GET" or not parsed.path.endswith("/messages") or not set(query).issubset({'limit', 'before'}):
            raise ValueError("Query is not allowed")
        if 'limit' in query and (len(query['limit']) != 1 or query['limit'][0] not in {'20', '40', '50', '100', '200', '1000'}):
            raise ValueError("Message limit is not allowed")
        if 'before' in query and (len(query['before']) != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{8,280}', query['before'][0])):
            raise ValueError('Invalid history cursor')
    body = value.get("body", {})
    if not isinstance(body, dict):
        raise ValueError("Connector body must be an object")
    key = value.get("key", "")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{8,180}", key):
        raise ValueError("A stable request key is required")
    return {"method": method, "path": path, "body": body, "key": key}
