import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/api.dart';
import 'support.dart';
import 'updates_test.dart' show Headers, Response;

class RelayRequest implements HttpClientRequest {
  @override
  final String method;
  final Uri url;
  final List<int> body = [];
  final Response response;
  RelayRequest(this.method, this.url, this.response);
  @override
  final headers = Headers();
  @override
  int contentLength = -1;
  @override
  bool followRedirects = true;
  @override
  void add(List<int> value) => body.addAll(value);
  @override
  Future<HttpClientResponse> close() async {
    if (method != 'GET') {
      expect(
        contentLength,
        body.length,
        reason: 'Python relay requires explicit UTF-8 byte length',
      );
      expect(contentLength, greaterThan(0));
    }
    expect(followRedirects, false);
    return response;
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class RelayResponse extends Response {
  @override
  final List<Cookie> cookies;
  RelayResponse(Json value, {this.cookies = const [], super.statusCode})
    : super([utf8.encode(jsonEncode(value))]);
}

class RelayClient implements HttpClient {
  final List<RelayResponse> responses;
  final requests = <RelayRequest>[];
  RelayClient(this.responses);
  @override
  Duration? connectionTimeout;
  @override
  Future<HttpClientRequest> openUrl(String method, Uri url) async {
    final request = RelayRequest(method, url, responses.removeAt(0));
    requests.add(request);
    return request;
  }

  @override
  void close({bool force = false}) {}
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  test(
    'event frames preserve the cursor across heartbeats and chunk boundaries',
    () async {
      final bytes = utf8.encode(
        'retry: 2000\r\n\r\n'
        'id: epoch:5\r\nevent: chat\r\ndata: {"threadId":"对话"}\r\n\r\n'
        ': heartbeat\n\nevent: device\ndata: {}\n\n'
        'id: epoch:8\nevent: reset\ndata: {}\n\n',
      );
      final chunks = Stream<List<int>>.fromIterable(
        bytes.map((byte) => [byte]),
      );
      final frames = await readRelayEvents(chunks).toList();
      expect(frames.map((frame) => frame.kind), ['chat', 'device', 'reset']);
      expect(frames.map((frame) => frame.id), [
        'epoch:5',
        'epoch:5',
        'epoch:8',
      ]);
      expect(frames.first.data['threadId'], '对话');
    },
  );
  test(
    'native registration establishes the session used by account actions',
    () async {
      final client = RelayClient([
        RelayResponse(
          {'username': 'lin', 'csrf': 'initial'},
          cookies: [Cookie('__Host-airemote_session', 'invited-session')],
        ),
        RelayResponse({'username': 'lin', 'csrf': 'current', 'device': {}}),
        RelayResponse({'code': 'pair-code'}),
      ]);
      final api = RelayApi(
        'https://example.com',
        MemoryVault(),
        httpClient: client,
      );
      final member = await api.register('invite-code', 'new-test-password');
      expect(member['username'], 'lin');
      expect(client.requests.first.url.path, '/api/register');
      expect(jsonDecode(utf8.decode(client.requests.first.body)), {
        'code': 'invite-code',
        'password': 'new-test-password',
      });
      await api.api('/api/account/pairings', method: 'POST');
      expect(client.requests.last.headers.value('X-AIRemote-CSRF'), 'current');
      expect(
        client.requests.last.headers.value(HttpHeaders.cookieHeader),
        '__Host-airemote_session=invited-session',
      );
      api.close();
    },
  );

  test(
    'UTF-8 login body uses Content-Length, persists cookie and sends CSRF',
    () async {
      final vault = MemoryVault();
      final client = RelayClient([
        RelayResponse(
          {'csrf': 'one'},
          cookies: [Cookie('__Host-airemote_session', 'synthetic-session')],
        ),
        RelayResponse({'csrf': 'two', 'device': {}}),
        RelayResponse({'ok': true}),
      ]);
      final api = RelayApi('https://example.com', vault, httpClient: client);
      await api.login('用户', 'test-only-密码');
      final body = jsonDecode(utf8.decode(client.requests.first.body));
      expect(body['username'], '用户');
      expect(
        client.requests.first.contentLength,
        greaterThan(utf8.decode(client.requests.first.body).length),
      );
      expect(
        client.requests[1].headers.value(HttpHeaders.cookieHeader),
        '__Host-airemote_session=synthetic-session',
      );
      await api.api('/api/logout', method: 'POST');
      expect(client.requests.last.headers.value('X-AIRemote-CSRF'), 'two');
      final restored = RelayApi(
        'https://example.com',
        vault,
        httpClient: RelayClient([]),
      );
      await restored.restore();
      expect(
        restored.cookieHeader,
        '__Host-airemote_session=synthetic-session',
      );
      api.close();
      restored.close();
    },
  );
}
