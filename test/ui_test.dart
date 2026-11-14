import 'dart:io';
import 'dart:ui' as ui;
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/platform.dart';
import 'package:airemote_mobile/core/updates.dart';
import 'package:airemote_mobile/ui/app.dart';
import 'package:airemote_mobile/ui/composer.dart';
import 'support.dart';

Future<void> renderEvidence(WidgetTester tester, String name) async {
  final directory = Platform.environment['AIREMOTE_QA_OUTPUT'];
  if (directory == null) return;
  await tester.runAsync(() async {
    await Future<void>.delayed(const Duration(milliseconds: 30));
  });
  await tester.pump();
  await tester.runAsync(() async {
    final boundary = tester.renderObject<RenderRepaintBoundary>(
      find.byKey(const Key('qa-frame')),
    );
    final image = await boundary.toImage(pixelRatio: 2);
    final bytes = await image.toByteData(format: ui.ImageByteFormat.png);
    await Directory(directory).create(recursive: true);
    await File(
      '$directory/$name.png',
    ).writeAsBytes(bytes!.buffer.asUint8List());
    image.dispose();
  });
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();
  setUpAll(() async {
    final material = FontLoader('MaterialIcons')
      ..addFont(rootBundle.load('fonts/MaterialIcons-Regular.otf'));
    await material.load();
    final font = Platform.environment['AIREMOTE_QA_FONT'];
    if (font != null) {
      final loader = FontLoader('Roboto')
        ..addFont(
          File(font).readAsBytes().then((bytes) => ByteData.sublistView(bytes)),
        );
      await loader.load();
      final mono = FontLoader('monospace')
        ..addFont(
          File(font).readAsBytes().then((bytes) => ByteData.sublistView(bytes)),
        );
      await mono.load();
    }
  });

  for (final size in [const Size(390, 844), const Size(320, 640)]) {
    testWidgets(
      'native chat and drawer cover the viewport and keep controls safe $size',
      (tester) async {
        tester.view.physicalSize = size;
        tester.view.devicePixelRatio = 1;
        tester.view.viewPadding = const FakeViewPadding(top: 24, bottom: 24);
        tester.view.padding = const FakeViewPadding(top: 24, bottom: 24);
        addTearDown(tester.view.reset);
        final state = demoWorkspace();
        addTearDown(state.dispose);
        await tester.pumpWidget(
          RepaintBoundary(
            key: const Key('qa-frame'),
            child: AIRemoteApp(
              workspace: state,
              updater: null,
              native: NativePlatform(),
              connect: (_) async {},
              changeServer: () {},
            ),
          ),
        );
        await tester.pump(const Duration(milliseconds: 500));
        expect(tester.takeException(), isNull);
        expect(
          tester.getRect(find.byKey(const Key('app-surface'))),
          Rect.fromLTWH(0, 0, size.width, size.height),
        );
        final goal = tester.getRect(find.byKey(const Key('goal-dock'))),
            composer = tester.getRect(
              find.byKey(const Key('message-composer')),
            );
        expect((goal.bottom - composer.top).abs(), lessThanOrEqualTo(1));
        expect(composer.bottom, lessThanOrEqualTo(size.height - 24));
        expect(find.text('20m 1s'), findsOneWidget);
        expect(find.text('已处理 1m 5s'), findsOneWidget);
        if (size.width == 390) {
          final userBubble = tester.getRect(find.textContaining('继续完善手机端'));
          expect(userBubble.center.dx, greaterThan(size.width / 2));
        }
        await renderEvidence(tester, 'flutter-chat-${size.width.toInt()}');
        await tester.tap(find.byTooltip('打开项目与对话'));
        await tester.pump(const Duration(milliseconds: 120));
        final controller = tester.widget<DrawerController>(
          find.byType(DrawerController),
        );
        expect(controller.scrimColor, Colors.black.withValues(alpha: .48));
        await renderEvidence(
          tester,
          'flutter-drawer-animation-${size.width.toInt()}',
        );
        await tester.pump(const Duration(milliseconds: 300));
        final drawer = tester.getRect(find.byKey(const Key('sidebar-surface')));
        expect(drawer.top, 0);
        expect(drawer.bottom, size.height);
        expect(drawer.width, lessThan(size.width));
        expect(drawer.width, closeTo(size.width * .82, .1));
        expect(
          tester.getRect(find.byType(Brand)).top,
          greaterThanOrEqualTo(24),
        );
        expect(
          tester.getRect(find.text('退出登录')).bottom,
          lessThanOrEqualTo(size.height - 24),
        );
        expect(find.text('我的私人工作台'), findsNothing);
        expect(tester.takeException(), isNull);
        await renderEvidence(tester, 'flutter-drawer-${size.width.toInt()}');
        await tester.tapAt(Offset(size.width - 8, size.height / 2));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 400));
        expect(state.drawerOpen, false);
        await tester.pumpWidget(const SizedBox.shrink());
      },
    );
  }
  testWidgets('keyboard resizes chat without duplicating bottom safe padding', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    tester.view.viewPadding = const FakeViewPadding(top: 24, bottom: 24);
    addTearDown(tester.view.reset);
    final state = demoWorkspace();
    addTearDown(state.dispose);
    await tester.pumpWidget(
      RepaintBoundary(
        key: const Key('qa-frame'),
        child: AIRemoteApp(
          workspace: state,
          updater: null,
          native: NativePlatform(),
          connect: (_) async {},
          changeServer: () {},
        ),
      ),
    );
    await tester.tap(find.byType(TextField).first);
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pump();
    final composer = tester.getRect(find.byKey(const Key('message-composer')));
    expect(composer.bottom, closeTo(844 - 300 - 8, 1));
    expect(tester.takeException(), isNull);
    await renderEvidence(tester, 'flutter-keyboard');
    await tester.pumpWidget(const SizedBox.shrink());
  });
  testWidgets('small keyboard viewport keeps a long draft and queue usable', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 640);
    tester.view.devicePixelRatio = 1;
    tester.view.viewPadding = const FakeViewPadding(top: 24, bottom: 24);
    addTearDown(tester.view.reset);
    final state = demoWorkspace()
      ..queue = [
        {
          'id': 'q1',
          'input': [
            {'text': '下一步验证文件下载'},
          ],
        },
        {
          'id': 'q2',
          'input': [
            {'text': '整理发布说明'},
          ],
        },
      ];
    addTearDown(state.dispose);
    state.setDraft('第一行指令\n第二行指令\n第三行指令\n第四行指令\n第五行指令');
    await tester.pumpWidget(
      RepaintBoundary(
        key: const Key('qa-frame'),
        child: AIRemoteApp(
          workspace: state,
          updater: null,
          native: NativePlatform(),
          connect: (_) async {},
          changeServer: () {},
        ),
      ),
    );
    await tester.tap(find.byType(TextField).first);
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.pump();
    expect(tester.takeException(), isNull);
    final send = tester.getRect(find.byTooltip('发送指令'));
    expect(send.bottom, lessThanOrEqualTo(640 - 280));
    await renderEvidence(tester, 'flutter-small-keyboard-queue');
    await tester.pumpWidget(const SizedBox.shrink());
  });
  testWidgets(
    'download progress persists across settings reopen with known and unknown lengths',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final updater = AppUpdater(MemoryVault())
        ..currentVersion = '0.8.0'
        ..stage = 'downloading'
        ..downloaded = 15728640
        ..total = 31457280
        ..speed = 1048576;
      final state = demoWorkspace();
      addTearDown(state.dispose);
      addTearDown(updater.dispose);
      await tester.pumpWidget(
        RepaintBoundary(
          key: const Key('qa-frame'),
          child: AIRemoteApp(
            workspace: state,
            updater: updater,
            native: NativePlatform(),
            connect: (_) async {},
            changeServer: () {},
          ),
        ),
      );
      await tester.tap(find.byTooltip('应用设置与更新'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('正在下载更新 · 50%'), findsOneWidget);
      expect(find.text('15.0 MB / 30.0 MB · 1.0 MB/s'), findsOneWidget);
      expect(
        tester
            .widget<LinearProgressIndicator>(
              find.byType(LinearProgressIndicator),
            )
            .value,
        .5,
      );
      await renderEvidence(tester, 'flutter-update-progress');
      await tester.tapAt(const Offset(200, 100));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      updater.total = null;
      updater.downloaded = 20000000;
      updater.changed();
      await tester.pump();
      await tester.tap(find.byTooltip('应用设置与更新'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.text('正在下载更新…'), findsOneWidget);
      expect(
        tester
            .widget<LinearProgressIndicator>(
              find.byType(LinearProgressIndicator),
            )
            .value,
        isNull,
      );
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
  testWidgets('goal form and queue are native controls attached to composer', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(390, 844);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.reset);
    final state = demoWorkspace()
      ..queue = [
        {
          'id': 'q1',
          'input': [
            {'text': '接下来验证文件下载'},
          ],
        },
        {
          'id': 'q2',
          'input': [
            {'text': '整理发布说明'},
          ],
        },
      ];
    addTearDown(state.dispose);
    await tester.pumpWidget(
      RepaintBoundary(
        key: const Key('qa-frame'),
        child: AIRemoteApp(
          workspace: state,
          updater: null,
          native: NativePlatform(),
          connect: (_) async {},
          changeServer: () {},
        ),
      ),
    );
    await tester.pump();
    expect(find.text('接下来验证文件下载'), findsOneWidget);
    expect(find.byTooltip('立即执行队列指令'), findsNWidgets(2));
    expect(tester.takeException(), isNull);
    await renderEvidence(tester, 'flutter-inline-queue');
    await tester.tap(find.textContaining('完成 AIRemote 手机客户端').first);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    expect(find.byType(GoalSheet), findsOneWidget);
    expect(find.text('修改目标'), findsOneWidget);
    await tester.tap(find.text('修改目标'));
    await tester.pump();
    expect(find.byType(TextField), findsNWidgets(3));
    expect(tester.takeException(), isNull);
    await renderEvidence(tester, 'flutter-goal-editor');
    await tester.pumpWidget(const SizedBox.shrink());
  });
}
