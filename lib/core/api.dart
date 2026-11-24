import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';

typedef Json = Map<String, dynamic>;
typedef JsonList = List<Json>;
JsonList objects(dynamic value) => (value is List ? value : const [])
    .whereType<Map>()
    .map((e) => Map<String, dynamic>.from(e))
    .toList();

abstract interface class Vault {
  Future<String?> read(String key);
  Future<void> write(String key, String value);
  Future<void> remove(String key);
}

String normalizeServer(String text) {
  final uri = Uri.tryParse(text.trim());
  if (uri == null ||
      uri.scheme != 'https' ||
      uri.host.isEmpty ||
      uri.userInfo.isNotEmpty ||
      uri.hasQuery ||
      uri.hasFragment ||
      (uri.path.isNotEmpty && uri.path != '/')) {
    throw const ApiError('请输入有效的 HTTPS 服务器根地址');
  }
  return uri.replace(path: '').toString();
}

String requestKey() {
  final random = Random.secure();
  return List.generate(
    16,
    (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
  ).join();
}

class ApiError implements Exception {
  final String message;
  final int status;
  final bool uncertain;
  const ApiError(this.message, [this.status = 0, this.uncertain = false]);
  @override
  String toString() => message;
}

abstract interface class RemoteApi {
  String get server;
  Future<Json> session();
  Future<Json> login(String username, String password);
  Future<Json> register(String code, String password);
  Future<void> logout();
  Future<Json> api(String path, {String method = 'GET', Json body = const {}});
  Future<Json> bridge(
    String path, {
    String method = 'GET',
    Json body = const {},
    String? key,
  });
  Future<Uri> artifactUrl(String path);
  String get cookieHeader;
  void close();
}

class RelayEvent {
  final String id, kind;
  final Json data;
  const RelayEvent(this.id, this.kind, this.data);
}

abstract interface class RelayEvents {
  Stream<RelayEvent> watchEvents(String provider, String thread, String cursor);
}

Stream<RelayEvent> readRelayEvents(Stream<List<int>> bytes) async* {
  var id = '', kind = '';
  List<String> data = [];
  await for (final line
      in bytes.transform(utf8.decoder).transform(const LineSplitter())) {
    if (line.isEmpty) {
      if (id.isNotEmpty && data.isNotEmpty) {
        final value = jsonDecode(data.join('\n'));
        if (value is Map<String, dynamic>) yield RelayEvent(id, kind, value);
      }
      kind = '';
      data = [];
    } else if (line.startsWith('id:')) {
      id = line.substring(3).trim();
    } else if (line.startsWith('event:')) {
      kind = line.substring(6).trim();
    } else if (line.startsWith('data:')) {
      data.add(line.substring(5).trimLeft());
    }
  }
}

class RelayApi implements RemoteApi, RelayEvents {
  @override
  final String server;
  final Vault vault;
  final HttpClient client;
  String _cookie = '', _csrf = '';
  int _sessionGeneration = 0;
  RelayApi(String address, this.vault, {HttpClient? httpClient})
    : server = normalizeServer(address),
      client = httpClient ?? HttpClient() {
    client.connectionTimeout = const Duration(seconds: 20);
  }
  String get _cookieKey => 'session:$server';
  Future<void> restore({String? legacyCookie}) async {
    _cookie = await vault.read(_cookieKey) ?? legacyCookie ?? '';
    if (_cookie.isNotEmpty) await vault.write(_cookieKey, _cookie);
  }

  @override
  String get cookieHeader => _cookie;

  @override
  Stream<RelayEvent> watchEvents(
    String provider,
    String thread,
    String cursor,
  ) {
    final eventsClient = HttpClient()
      ..connectionTimeout = const Duration(seconds: 10);
    final generation = _sessionGeneration;
    final events = StreamController<RelayEvent>(
      onCancel: () => eventsClient.close(force: true),
    );
    events.onListen = () async {
      try {
        final url = Uri.parse('$server/api/events').replace(
          queryParameters: {
            'provider': provider,
            if (thread.isNotEmpty) 'thread': thread,
          },
        );
        final request = await eventsClient.getUrl(url);
        _ensureSession(generation);
        request.followRedirects = false;
        request.headers.set(HttpHeaders.acceptHeader, 'text/event-stream');
        request.headers.set(HttpHeaders.cookieHeader, _cookie);
        if (cursor.isNotEmpty) request.headers.set('Last-Event-ID', cursor);
        final response = await request.close().timeout(
          const Duration(seconds: 12),
        );
        _ensureSession(generation);
        if (response.statusCode != 200 ||
            response.headers.contentType?.mimeType != 'text/event-stream') {
          throw ApiError('暂时无法连接同步服务', response.statusCode);
        }
        await for (final item in readRelayEvents(
          response.timeout(const Duration(seconds: 22)),
        )) {
          _ensureSession(generation);
          if (!events.hasListener) break;
          events.add(item);
        }
      } catch (e, stack) {
        if (events.hasListener) events.addError(e, stack);
      } finally {
        eventsClient.close(force: true);
        unawaited(events.close());
      }
    };
    return events.stream;
  }

  void _ensureSession(int generation) {
    if (generation != _sessionGeneration) {
      throw const ApiError('登录状态已变化。', 409);
    }
  }

  Future<(int, List<int>)> _http(
    String path, {
    String method = 'GET',
    Json body = const {},
  }) async {
    if (!path.startsWith('/api/') || path.startsWith('//')) {
      throw const ApiError('无效的服务器请求');
    }
    final generation = _sessionGeneration;
    try {
      final request = await client.openUrl(method, Uri.parse('$server$path'));
      _ensureSession(generation);
      request.followRedirects = false;
      request.headers.contentType = ContentType.json;
      if (_cookie.isNotEmpty) {
        request.headers.set(HttpHeaders.cookieHeader, _cookie);
      }
      if (_csrf.isNotEmpty) request.headers.set('X-AIRemote-CSRF', _csrf);
      if (method != 'GET') {
        // The relay reads a bounded Content-Length body, not chunked uploads.
        final payload = utf8.encode(jsonEncode(body));
        request.contentLength = payload.length;
        request.add(payload);
      }
      final response = await request.close().timeout(
        const Duration(seconds: 40),
      );
      _ensureSession(generation);
      for (final cookie in response.cookies) {
        if (cookie.name != '__Host-airemote_session' &&
            cookie.name != 'airemote_session') {
          continue;
        }
        _cookie = cookie.value.isEmpty || cookie.maxAge == 0
            ? ''
            : '${cookie.name}=${cookie.value}';
        if (_cookie.isEmpty) {
          await vault.remove(_cookieKey);
        } else {
          await vault.write(_cookieKey, _cookie);
        }
      }
      final bytes = <int>[];
      await for (final chunk in response.timeout(const Duration(seconds: 40))) {
        if (bytes.length + chunk.length > 2 * 1024 * 1024) {
          throw const ApiError('服务器响应过大');
        }
        bytes.addAll(chunk);
      }
      _ensureSession(generation);
      return (response.statusCode, bytes);
    } on ApiError {
      rethrow;
    } on HandshakeException {
      throw const ApiError('服务器证书验证失败，请检查 HTTPS 配置');
    } on SocketException {
      throw const ApiError('网络未连接，请检查网络后重试');
    } on TimeoutException {
      throw const ApiError('服务器响应超时，请检查连接');
    }
  }

  @override
  Future<Json> api(
    String path, {
    String method = 'GET',
    Json body = const {},
  }) async {
    final (status, bytes) = await _http(path, method: method, body: body);
    Json value;
    try {
      value = Map<String, dynamic>.from(jsonDecode(utf8.decode(bytes)));
    } catch (_) {
      throw ApiError('服务器返回了无效响应 ($status)', status);
    }
    if (status >= 400) {
      throw ApiError(
        value['error']?.toString() ?? '请求失败 ($status)',
        status,
        status >= 500 ||
            RegExp(
              '不确定|未知|outcome is unknown|缓存已过期',
              caseSensitive: false,
            ).hasMatch(value['error']?.toString() ?? ''),
      );
    }
    return value;
  }

  @override
  Future<Json> session() async {
    final value = await api('/api/session');
    _csrf = value['csrf'] as String? ?? '';
    return value;
  }

  @override
  Future<Json> login(String username, String password) =>
      _authenticate('/api/login', {'username': username, 'password': password});

  @override
  Future<Json> register(String code, String password) =>
      _authenticate('/api/register', {'code': code, 'password': password});

  Future<Json> _authenticate(String path, Json body) async {
    final value = await api(path, method: 'POST', body: body);
    _sessionGeneration++;
    _csrf = value['csrf'] as String? ?? '';
    return session();
  }

  @override
  Future<void> logout() async {
    await api('/api/logout', method: 'POST');
    _sessionGeneration++;
    _csrf = '';
    _cookie = '';
    await vault.remove(_cookieKey);
  }

  Future<String> _job(String path, String method, Json body, String key) async {
    // Retry only a lost submission, with the identical relay idempotency key.
    final generation = _sessionGeneration;
    for (var attempt = 0; ; attempt++) {
      _ensureSession(generation);
      try {
        return (await api(
              '/api/requests',
              method: 'POST',
              body: {'method': method, 'path': path, 'body': body, 'key': key},
            ))['id']
            as String;
      } on ApiError catch (e) {
        if (attempt > 0 || e.status != 0) rethrow;
        await Future<void>.delayed(const Duration(milliseconds: 600));
      }
    }
  }

  Future<String> _completedJob(
    String path,
    String method,
    Json body,
    String key,
  ) async {
    final generation = _sessionGeneration;
    final id = await _job(path, method, body, key);
    final end = DateTime.now().add(const Duration(seconds: 145));
    while (DateTime.now().isBefore(end)) {
      _ensureSession(generation);
      final value = await api('/api/requests/$id');
      if (value['state'] == 'done') return id;
      if (['expired', 'unknown', 'result_expired'].contains(value['state'])) {
        throw ApiError(
          value['state'] == 'expired'
              ? '请求已过期，未执行，请重试'
              : '操作结果暂不确定，请先刷新核对，避免重复执行',
          409,
          value['state'] != 'expired',
        );
      }
      await Future<void>.delayed(const Duration(milliseconds: 500));
    }
    throw const ApiError('仍在等待 Mac 的结果，请先回读对话，避免重复执行', 504, true);
  }

  @override
  Future<Json> bridge(
    String path, {
    String method = 'GET',
    Json body = const {},
    String? key,
  }) async {
    final id = await _completedJob(path, method, body, key ?? requestKey());
    return api('/api/requests/$id/result');
  }

  @override
  Future<Uri> artifactUrl(String path) async {
    final id = await _completedJob(path, 'GET', const {}, requestKey());
    return Uri.parse('$server/api/requests/$id/result');
  }

  @override
  void close() => client.close(force: true);
}
