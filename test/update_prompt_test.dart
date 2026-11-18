import 'dart:async';
import 'dart:io';

import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/platform.dart';
import 'package:airemote_mobile/core/updates.dart';
import 'package:airemote_mobile/ui/app.dart';
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:package_info_plus/package_info_plus.dart';

import 'support.dart';
import 'updates_test.dart' show Client;

class UpdatePlatform extends NativePlatform {
  @override
  final bool supportsApkUpdates;
  int verifications = 0, installations = 0;
  bool permission = false;

  UpdatePlatform({this.supportsApkUpdates = true});

  @override
  Future<PackageInfo> appInfo() async => PackageInfo(
    appName: 'AIRemote',
    packageName: 'fun.winlife.airemote',
    version: '0.10.2',
    buildNumber: '10002',
  );

  @override
  Future<void> verifyUpdate(File file) async => verifications++;

  @override
  Future<String> installUpdate(File file) async {
    installations++;
    return permission ? 'installing' : 'permission-required';
  }

  @override
  Future<bool> canInstallUpdates() async => permission;
}

class UpdatesFixture extends GitHubReleases {
  String? version;
  final finishDownload = Completer<void>();
  int checks = 0;

  UpdatesFixture(this.version) : super(httpClient: Client([]));

  @override
  Future<AppRelease> latest() async {
    checks++;
    if (version == null) throw const ApiError('连接超时');
    return AppRelease(
      version!,
      Uri.https('github.com', '/fixture.apk'),
      'fixture',
      100,
    );
  }

  @override
  Future<File> download(
    AppRelease release,
    Directory directory,
    void Function(int, int?, double) progress, {
    void Function()? verifying,
  }) async {
    progress(25, 100, 5);
    await finishDownload.future;
    verifying?.call();
    return File('${directory.path}/fixture.apk');
  }
}

Widget shell(AppUpdater updater, {bool starting = false}) => AIRemoteApp(
  workspace: null,
  updater: updater,
  native: updater.platform,
  starting: starting,
  connect: (_) async {},
  changeServer: () {},
);

void main() {
  testWidgets(
    'cold start ignores the former cooldown and offers each new version once',
    (tester) async {
      final vault = MemoryVault()
        ..values['last-update-check'] =
            '${DateTime.now().millisecondsSinceEpoch}';
      final source = UpdatesFixture('v0.10.3');
      final updater = AppUpdater(
        vault,
        releases: source,
        native: UpdatePlatform(),
      );
      addTearDown(updater.dispose);
      await tester.pumpWidget(shell(updater, starting: true));
      await updater.initialize();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(source.checks, 1);
      expect(find.widgetWithText(AlertDialog, '发现新版本 v0.10.3'), findsOneWidget);
      expect(find.text('当前版本 0.10.2'), findsOneWidget);
      await updater.resumed();
      expect(source.checks, 1);

      await tester.tap(find.text('稍后'));
      await tester.pumpWidget(shell(updater));
      await tester.pumpAndSettle();
      expect(find.text('连接服务器'), findsOneWidget);
      expect(find.byType(AlertDialog), findsNothing);
      await updater.check(automatic: true);
      await tester.pumpAndSettle();
      expect(find.byType(AlertDialog), findsNothing);
      expect(updater.stage, 'available');
    },
  );

  testWidgets(
    'the startup dialog shows progress through signing and installation',
    (tester) async {
      tester.view.physicalSize = const Size(360, 720);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final api = UpdatePlatform();
      final source = UpdatesFixture('v0.10.3');
      final updater = AppUpdater(MemoryVault(), releases: source, native: api);
      addTearDown(updater.dispose);
      const pathProvider = MethodChannel('plugins.flutter.io/path_provider');
      final messenger = tester.binding.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(
        pathProvider,
        (_) async => '/tmp/airemote-fixture',
      );
      addTearDown(() => messenger.setMockMethodCallHandler(pathProvider, null));
      await updater.initialize();
      await tester.pumpWidget(shell(updater));
      await tester.pumpAndSettle();
      await tester.tap(find.text('下载更新'));
      await tester.pumpAndSettle();
      expect(find.text('正在下载更新 · 25%'), findsOneWidget);
      expect(find.text('25 B / 100 B · 5 B/s'), findsOneWidget);
      expect(
        tester
            .widget<LinearProgressIndicator>(
              find.byType(LinearProgressIndicator),
            )
            .value,
        .25,
      );
      expect(api.installations, 0);

      source.finishDownload.complete();
      await tester.pumpAndSettle();
      expect(api.verifications, 1);
      expect(find.text('下载与校验完成，可以安装'), findsOneWidget);
      await tester.tap(find.text('安装更新'));
      await tester.pumpAndSettle();
      expect(find.text('打开安装权限设置'), findsOneWidget);
      api.permission = true;
      await updater.resumed();
      await tester.pumpAndSettle();
      await tester.tap(find.text('安装更新'));
      await tester.pumpAndSettle();
      expect(api.installations, 2);
      expect(find.text('等待系统确认安装'), findsOneWidget);
    },
  );

  testWidgets(
    'disabled, unsupported, current, unavailable and manual checks stay silent',
    (tester) async {
      for (final (enabled, android, version, count, stage) in [
        (false, true, 'v0.10.3', 0, 'idle'),
        (true, false, 'v0.10.3', 0, 'unsupported'),
        (true, true, 'v0.10.2', 1, 'latest'),
        (true, true, 'v0.10.1', 1, 'latest'),
        (true, true, null, 1, 'error'),
      ]) {
        final vault = MemoryVault()..values['auto-update'] = '$enabled';
        final source = UpdatesFixture(version);
        final updater = AppUpdater(
          vault,
          releases: source,
          native: UpdatePlatform(supportsApkUpdates: android),
        );
        addTearDown(updater.dispose);
        await updater.initialize();
        await tester.pumpWidget(shell(updater));
        await tester.pumpAndSettle();
        expect(source.checks, count);
        expect(updater.stage, stage);
        expect(find.byType(AlertDialog), findsNothing);
        if (version == 'v0.10.2') {
          source.version = 'v0.10.3';
          await updater.check();
          await tester.pumpAndSettle();
          expect(source.checks, 2);
          expect(updater.stage, 'available');
          expect(find.byType(AlertDialog), findsNothing);
        }
      }
    },
  );
}
