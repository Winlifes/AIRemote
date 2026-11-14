import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter/foundation.dart';
import 'package:path_provider/path_provider.dart';
import 'api.dart';
import 'platform.dart';

int versionCode(String value) {
  final match = RegExp(
    r'^v?(\d{1,3})\.(\d{1,3})\.(\d{1,3})$',
  ).firstMatch(value);
  return match == null
      ? -1
      : int.parse(match.group(1)!) * 1000000 +
            int.parse(match.group(2)!) * 1000 +
            int.parse(match.group(3)!);
}

String byteSize(num bytes) => bytes < 1024
    ? '${bytes.floor()} B'
    : bytes < 1024 * 1024
    ? '${(bytes / 1024).toStringAsFixed(1)} KB'
    : '${(bytes / (1024 * 1024)).toStringAsFixed(1)} MB';

class AppRelease {
  final String version, digest;
  final Uri url;
  final int? size;
  const AppRelease(this.version, this.url, this.digest, this.size);
}

class GitHubReleases {
  static const repo = 'Winlifes/AIRemote';
  final HttpClient client;
  GitHubReleases({HttpClient? httpClient})
    : client = httpClient ?? HttpClient() {
    client.connectionTimeout = const Duration(seconds: 20);
    client.autoUncompress = false;
  }
  static void trusted(Uri uri) {
    if (uri.scheme != 'https' ||
        uri.userInfo.isNotEmpty ||
        (uri.hasPort && uri.port != 443) ||
        !(uri.host == 'github.com' ||
            uri.host == 'api.github.com' ||
            uri.host.endsWith('.githubusercontent.com'))) {
      throw const ApiError('更新地址不是受信任的 GitHub HTTPS 地址');
    }
  }

  Future<HttpClientResponse> open(Uri uri) async {
    for (var n = 0; n < 6; n++) {
      trusted(uri);
      final request = await client.getUrl(uri);
      request.followRedirects = false;
      request.headers.set(
        HttpHeaders.userAgentHeader,
        'AIRemote-Flutter-Updater',
      );
      request.headers.set(
        HttpHeaders.acceptHeader,
        'application/vnd.github+json',
      );
      final response = await request.close().timeout(
        const Duration(seconds: 30),
      );
      if (response.isRedirect) {
        final next = response.headers.value(HttpHeaders.locationHeader);
        await response.drain<void>();
        if (next == null) throw const ApiError('GitHub 返回了无效跳转');
        uri = uri.resolve(next);
        continue;
      }
      if (response.statusCode != 200) {
        await response.drain<void>();
        throw ApiError('GitHub 暂不可用 (${response.statusCode})');
      }
      return response;
    }
    throw const ApiError('更新地址跳转过多');
  }

  Future<String> text(Uri uri, int maximum) async {
    final response = await open(uri);
    final bytes = <int>[];
    await for (final chunk in response.timeout(const Duration(seconds: 30))) {
      if (bytes.length + chunk.length > maximum) {
        throw const ApiError('更新元数据过大');
      }
      bytes.addAll(chunk);
    }
    return utf8.decode(bytes);
  }

  Future<AppRelease> latest() async {
    final json =
        jsonDecode(
              await text(
                Uri.parse('https://api.github.com/repos/$repo/releases/latest'),
                2 * 1024 * 1024,
              ),
            )
            as Json;
    final version = json['tag_name'] as String;
    if (versionCode(version) < 0 ||
        json['draft'] == true ||
        json['prerelease'] == true) {
      throw const ApiError('未找到稳定版本');
    }
    final assets = objects(json['assets']);
    final apk = assets
        .where((v) => v['name'] == 'AIRemote-android.apk')
        .firstOrNull;
    if (apk == null) throw const ApiError('此版本缺少安卓安装包');
    final expected = 'https://github.com/$repo/releases/download/$version/';
    if (apk['browser_download_url'] != '${expected}AIRemote-android.apk') {
      throw const ApiError('安装包不属于当前仓库版本');
    }
    var digest = apk['digest']?.toString() ?? '';
    if (RegExp(r'^sha256:[a-fA-F0-9]{64}$').hasMatch(digest)) {
      digest = digest.substring(7);
    } else {
      final manifest = assets
          .where((v) => v['name'] == 'SHA256SUMS')
          .firstOrNull;
      if (manifest?['browser_download_url'] != '${expected}SHA256SUMS') {
        throw const ApiError('安装包缺少校验文件');
      }
      final lines = (await text(
        Uri.parse('${expected}SHA256SUMS'),
        65536,
      )).split('\n');
      digest = '';
      for (final line in lines) {
        final parts = line.trim().split(RegExp(r'\s+'));
        if (parts.length == 2 &&
            parts[1].replaceFirst('*', '') == 'AIRemote-android.apk') {
          digest = parts[0];
        }
      }
    }
    if (!RegExp(r'^[a-fA-F0-9]{64}$').hasMatch(digest)) {
      throw const ApiError('安装包缺少有效 SHA-256 校验值');
    }
    return AppRelease(
      version,
      Uri.parse(apk['browser_download_url']),
      digest.toLowerCase(),
      apk['size'] is int && apk['size'] > 0 ? apk['size'] : null,
    );
  }

  Future<File> download(
    AppRelease release,
    Directory directory,
    void Function(int, int?, double) progress, {
    void Function()? verifying,
  }) async {
    await directory.create(recursive: true);
    final partial = File('${directory.path}/AIRemote-update.partial'),
        target = File('${directory.path}/AIRemote-update.apk');
    const maximum = 160 * 1024 * 1024;
    try {
      final response = await open(release.url);
      final total = response.contentLength > 0
          ? response.contentLength
          : release.size;
      if ((total ?? 0) > maximum) throw const ApiError('更新安装包超过大小限制');
      final clock = Stopwatch()..start();
      var downloaded = 0, last = 0;
      progress(0, total, 0);
      final sink = partial.openWrite();
      try {
        await for (final chunk in response.timeout(
          const Duration(seconds: 45),
        )) {
          downloaded += chunk.length;
          if (downloaded > maximum) throw const ApiError('更新安装包超过大小限制');
          sink.add(chunk);
          if (clock.elapsedMilliseconds - last >= 100) {
            progress(
              downloaded,
              total,
              downloaded / (clock.elapsedMilliseconds.clamp(1, 1 << 40) / 1000),
            );
            last = clock.elapsedMilliseconds;
          }
        }
      } finally {
        await sink.close();
      }
      progress(
        downloaded,
        total,
        downloaded / (clock.elapsedMilliseconds.clamp(1, 1 << 40) / 1000),
      );
      verifying?.call();
      final actual = await sha256.bind(partial.openRead()).first;
      if (actual.toString() != release.digest) {
        throw const ApiError('安装包校验失败，已停止安装');
      }
      if (await target.exists()) await target.delete();
      return await partial.rename(target.path);
    } catch (_) {
      if (await partial.exists()) await partial.delete();
      rethrow;
    }
  }

  void close() => client.close(force: true);
}

class AppUpdater extends ChangeNotifier {
  final GitHubReleases source;
  final NativePlatform platform;
  final Vault vault;
  String stage = 'idle', error = '', currentVersion = '';
  int downloaded = 0;
  int? total;
  double speed = 0;
  AppRelease? release;
  File? apk;
  bool autoCheck = true, disposed = false;
  AppUpdater(this.vault, {GitHubReleases? releases, NativePlatform? native})
    : source = releases ?? GitHubReleases(),
      platform = native ?? NativePlatform();
  bool get busy => [
    'checking',
    'connecting',
    'downloading',
    'verifying',
    'verifying_app',
  ].contains(stage);
  double? get fraction =>
      total != null && total! > 0 ? (downloaded / total!).clamp(0, 1) : null;
  String get label => switch (stage) {
    'checking' => '正在检查 GitHub 新版本…',
    'available' => '发现新版本 ${release?.version}',
    'latest' => '已是最新版本 $currentVersion',
    'connecting' => '正在连接下载源…',
    'downloading' =>
      fraction == null ? '正在下载更新…' : '正在下载更新 · ${(fraction! * 100).floor()}%',
    'verifying' => '正在校验下载文件…',
    'verifying_app' => '正在验证安装包签名…',
    'ready' => '下载与校验完成，可以安装',
    'permission' => '请允许安装应用更新',
    'installing' => '等待系统确认安装',
    'unsupported' => 'iOS 版本通过 App Store 或 TestFlight 更新',
    'error' => '更新未完成',
    _ => '检查应用更新',
  };
  String get detail =>
      '${total == null ? '已下载 ${byteSize(downloaded)}' : '${byteSize(downloaded)} / ${byteSize(total!)}'}${speed > 0 ? ' · ${byteSize(speed)}/s' : ''}';
  void changed() {
    if (!disposed) notifyListeners();
  }

  Future<void> initialize() async {
    final info = await platform.appInfo();
    currentVersion = info.version;
    autoCheck = await vault.read('auto-update') != 'false';
    changed();
    final last = int.tryParse(await vault.read('last-update-check') ?? '') ?? 0;
    if (autoCheck &&
        DateTime.now().millisecondsSinceEpoch - last >
            const Duration(hours: 6).inMilliseconds &&
        Platform.isAndroid) {
      check();
    }
  }

  Future<void> setAuto(bool value) async {
    autoCheck = value;
    await vault.write('auto-update', '$value');
    changed();
  }

  Future<void> check() async {
    if (busy) return;
    if (Platform.isIOS) {
      stage = 'unsupported';
      changed();
      return;
    }
    stage = 'checking';
    error = '';
    changed();
    try {
      release = await source.latest();
      await vault.write(
        'last-update-check',
        '${DateTime.now().millisecondsSinceEpoch}',
      );
      stage = versionCode(release!.version) > versionCode(currentVersion)
          ? 'available'
          : 'latest';
    } catch (e) {
      error = '$e';
      stage = 'error';
    }
    changed();
  }

  Future<void> download() async {
    if (busy || release == null) return;
    stage = 'connecting';
    downloaded = 0;
    total = release!.size;
    speed = 0;
    error = '';
    changed();
    try {
      final cache = await getTemporaryDirectory();
      apk = await source.download(
        release!,
        Directory('${cache.path}/updates'),
        (bytes, maximum, rate) {
          downloaded = bytes;
          total = maximum;
          speed = rate;
          stage = 'downloading';
          changed();
        },
        verifying: () {
          stage = 'verifying';
          changed();
        },
      );
      stage = 'verifying_app';
      changed();
      await platform.verifyUpdate(apk!);
      stage = 'ready';
    } catch (e) {
      stage = 'error';
      error = '$e';
    }
    changed();
  }

  Future<void> install() async {
    if (apk == null || busy) return;
    try {
      stage = await platform.installUpdate(apk!) == 'permission-required'
          ? 'permission'
          : 'installing';
    } catch (e) {
      error = '$e';
      stage = 'error';
    }
    changed();
  }

  Future<void> resumed() async {
    if (stage != 'permission' || !Platform.isAndroid) return;
    try {
      if (await platform.canInstallUpdates()) {
        stage = 'ready';
        changed();
      }
    } catch (e) {
      error = '$e';
      changed();
    }
  }

  @override
  void dispose() {
    disposed = true;
    source.close();
    super.dispose();
  }
}
