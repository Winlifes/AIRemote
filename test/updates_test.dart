import 'dart:async';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/updates.dart';

class Headers implements HttpHeaders {
  final values = <String, String>{};
  @override
  ContentType? contentType;
  @override
  String? value(String name) => values[name];
  @override
  void set(String name, Object value, {bool preserveHeaderCase = false}) {
    values[name] = '$value';
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class Response extends Stream<List<int>> implements HttpClientResponse {
  final List<List<int>> chunks;
  @override
  final int contentLength, statusCode;
  @override
  final Headers headers;
  Response(
    this.chunks, {
    this.contentLength = -1,
    this.statusCode = 200,
    Headers? headers,
  }) : headers = headers ?? Headers();
  @override
  bool get isRedirect => statusCode == 302;
  @override
  StreamSubscription<List<int>> listen(
    void Function(List<int>)? onData, {
    Function? onError,
    void Function()? onDone,
    bool? cancelOnError,
  }) => _stream().listen(
    onData,
    onError: onError,
    onDone: onDone,
    cancelOnError: cancelOnError,
  );
  Stream<List<int>> _stream() async* {
    for (final chunk in chunks) {
      await Future<void>.delayed(const Duration(milliseconds: 110));
      yield chunk;
    }
  }

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class Request implements HttpClientRequest {
  final Response response;
  Request(this.response);
  @override
  final headers = Headers();
  @override
  bool followRedirects = false;
  @override
  Future<HttpClientResponse> close() async => response;
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class Client implements HttpClient {
  final List<Response> replies;
  final urls = <Uri>[];
  Client(this.replies);
  @override
  Duration? connectionTimeout;
  @override
  bool autoUncompress = true;
  @override
  Future<HttpClientRequest> getUrl(Uri url) async {
    urls.add(url);
    return Request(replies.removeAt(0));
  }

  @override
  void close({bool force = false}) {}
  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

void main() {
  test('version codes preserve the existing Android upgrade ordering', () {
    expect(versionCode('v0.8.0'), 8000);
    expect(versionCode('0.8.0'), greaterThan(versionCode('0.7.1')));
    expect(versionCode('0.8.0-beta'), -1);
    expect(versionCode('../0.8.0'), -1);
  });
  for (final known in [true, false]) {
    test(
      'download emits live byte progress before verification (known size: $known)',
      () async {
        final directory = await Directory.systemTemp.createTemp(
          'airemote-update-',
        );
        addTearDown(() => directory.delete(recursive: true));
        final data = List<int>.generate(9000, (n) => n % 256);
        final source = GitHubReleases(
          httpClient: Client([
            Response([
              data.sublist(0, 3000),
              data.sublist(3000, 6000),
              data.sublist(6000),
            ], contentLength: known ? 9000 : -1),
          ]),
        );
        final events = <(int, int?)>[], stages = <String>[];
        final release = AppRelease(
          'v0.8.0',
          Uri.parse(
            'https://github.com/Winlifes/AIRemote/releases/download/v0.8.0/AIRemote-android.apk',
          ),
          sha256.convert(data).toString(),
          null,
        );
        final output = await source.download(
          release,
          directory,
          (bytes, total, speed) {
            events.add((bytes, total));
            stages.add('download');
            expect(speed, greaterThanOrEqualTo(0));
          },
          verifying: () {
            stages.add('verify');
          },
        );
        expect(events.first, (0, known ? 9000 : null));
        expect(events.last, (9000, known ? 9000 : null));
        expect(events.any((e) => e.$1 > 0 && e.$1 < 9000), true);
        expect(stages.last, 'verify');
        expect(await output.readAsBytes(), data);
        expect(
          await File('${directory.path}/AIRemote-update.partial').exists(),
          false,
        );
      },
    );
  }
  test(
    'checksum failure removes partial file and never prepares an installable APK',
    () async {
      final directory = await Directory.systemTemp.createTemp(
        'airemote-bad-update-',
      );
      addTearDown(() => directory.delete(recursive: true));
      final source = GitHubReleases(
        httpClient: Client([
          Response([
            [1, 2, 3],
          ]),
        ]),
      );
      final release = AppRelease(
        'v0.8.0',
        Uri.parse(
          'https://github.com/Winlifes/AIRemote/releases/download/v0.8.0/AIRemote-android.apk',
        ),
        '0' * 64,
        3,
      );
      await expectLater(
        source.download(release, directory, (_, _, _) {}),
        throwsA(isA<ApiError>()),
      );
      expect(await directory.list().toList(), isEmpty);
    },
  );
  test('redirects never follow untrusted origins', () async {
    final headers = Headers()
      ..set('location', 'https://github.com.attacker.example/file.apk');
    final client = Client([Response([], statusCode: 302, headers: headers)]);
    final source = GitHubReleases(httpClient: client);
    await expectLater(
      source.open(Uri.parse('https://github.com/Winlifes/AIRemote')),
      throwsA(isA<ApiError>()),
    );
    expect(client.urls, hasLength(1));
  });
}
