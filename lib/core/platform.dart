import 'dart:io';
import 'package:flutter/services.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path_provider/path_provider.dart';
import 'package:share_plus/share_plus.dart';
import 'api.dart';

class SecureVault implements Vault {
  final FlutterSecureStorage storage;
  const SecureVault([this.storage = const FlutterSecureStorage()]);
  @override
  Future<String?> read(String key) => storage.read(key: key);
  @override
  Future<void> write(String key, String value) =>
      storage.write(key: key, value: value);
  @override
  Future<void> remove(String key) => storage.delete(key: key);
}

class NativePlatform {
  static const channel = MethodChannel('fun.winlife.airemote/native');
  bool get supportsApkUpdates => Platform.isAndroid;

  Future<Json> legacySession() async {
    if (!Platform.isAndroid) return {};
    return Map<String, dynamic>.from(
      await channel.invokeMapMethod<String, dynamic>('legacySession') ?? {},
    );
  }

  Future<void> downloadFile(
    RemoteApi api,
    Json file,
    Uri address, {
    Rect? shareOrigin,
  }) async {
    if (address.origin != Uri.parse(api.server).origin ||
        !RegExp(
          r'^/api/requests/[0-9a-f-]{36}/result$',
        ).hasMatch(address.path)) {
      throw const ApiError('下载地址不属于当前服务器');
    }
    if (Platform.isAndroid) {
      await channel.invokeMethod<void>('downloadFile', {
        'url': address.toString(),
        'server': api.server,
        'cookie': api.cookieHeader,
        'name': file['name'],
        'mime': 'application/octet-stream',
      });
      return;
    }
    final http = HttpClient()..connectionTimeout = const Duration(seconds: 20);
    File? output;
    try {
      final request = await http.getUrl(address);
      request.followRedirects = false;
      request.headers.set(HttpHeaders.cookieHeader, api.cookieHeader);
      final response = await request.close();
      if (response.statusCode != 200) throw const ApiError('文件暂时无法下载，请刷新后重试');
      final directory = await getTemporaryDirectory();
      var name = '${file['name'] ?? 'AIRemote-file'}'.replaceAll(
        RegExp(r'[/\\\x00-\x1f]'),
        '_',
      );
      if (name.startsWith('.')) name = 'file-$name';
      output = File('${directory.path}/${requestKey()}-$name');
      final sink = output.openWrite();
      var bytes = 0;
      try {
        await for (final chunk in response.timeout(
          const Duration(seconds: 40),
        )) {
          bytes += chunk.length;
          if (bytes > 64 * 1024 * 1024) throw const ApiError('文件超过下载大小限制');
          sink.add(chunk);
        }
      } finally {
        await sink.close();
      }
      await SharePlus.instance.share(
        ShareParams(
          files: [XFile(output.path)],
          title: name,
          sharePositionOrigin: shareOrigin,
        ),
      );
    } catch (_) {
      if (output != null && await output.exists()) await output.delete();
      rethrow;
    } finally {
      http.close(force: true);
    }
  }

  Future<void> verifyUpdate(File file) =>
      channel.invokeMethod<void>('verifyUpdate', {'path': file.path});
  Future<String> installUpdate(File file) async =>
      await channel.invokeMethod<String>('installUpdate', {
        'path': file.path,
      }) ??
      '';
  Future<void> openInstallPermission() =>
      channel.invokeMethod<void>('openInstallPermission');
  Future<bool> canInstallUpdates() async =>
      await channel.invokeMethod<bool>('canInstallUpdates') ?? false;
  Future<PackageInfo> appInfo() => PackageInfo.fromPlatform();
}
