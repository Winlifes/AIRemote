import 'dart:async';
import 'dart:io';
import 'dart:ui' as ui;
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/platform.dart';
import 'package:airemote_mobile/core/updates.dart';
import 'package:airemote_mobile/core/workspace.dart';
import 'package:airemote_mobile/ui/app.dart';
import 'package:airemote_mobile/ui/chat_transcript.dart';
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
  testWidgets(
    'the sidebar shows project loading and keeps rows during refresh',
    (tester) async {
      final codex = Completer<Json>(), deepseek = Completer<Json>();
      final refresh = Completer<Json>();
      var deepseekReads = 0;
      final api = FakeApi()
        ..respond = (call) => switch (call.path) {
          '/api/projects' => codex.future,
          '/api/providers/deepseek/projects' =>
            deepseekReads++ == 0 ? deepseek.future : refresh.future,
          _ => FakeApi.defaultResponse(call),
        };
      final state = Workspace(api);
      addTearDown(state.dispose);
      final started = state.start();
      await tester.pumpWidget(
        AIRemoteApp(
          workspace: state,
          updater: null,
          native: NativePlatform(),
          connect: (_) async {},
          changeServer: () {},
        ),
      );
      await tester.pump();
      await tester.tap(find.byTooltip('打开项目与对话'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 350));
      final loading = find.text('正在加载项目…');
      expect(loading, findsOneWidget);

      await tester.tap(find.bySemanticsLabel('切换到 DeepSeek'));
      codex.complete({
        'projects': [
          {'id': 'codex', 'name': 'Codex 项目'},
        ],
      });
      await tester.pump();
      expect(loading, findsOneWidget);
      expect(find.text('Codex 项目'), findsNothing);

      final data = {
        'projects': [
          {'id': 'ds', 'name': 'DeepSeek 项目'},
        ],
      };
      deepseek.complete(data);
      await tester.pump();
      expect(loading, findsNothing);
      expect(find.text('DeepSeek 项目'), findsOneWidget);

      await tester.tap(find.byTooltip('刷新项目列表'));
      await tester.pump();
      expect(find.byTooltip('正在刷新项目列表'), findsOneWidget);
      expect(find.text('DeepSeek 项目'), findsOneWidget);
      refresh.complete(data);
      await tester.pump();
      expect(find.byTooltip('刷新项目列表'), findsOneWidget);
      await started;
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'composer morph keeps one focused input and preserves its draft',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      tester.view.viewPadding = const FakeViewPadding(top: 24, bottom: 24);
      addTearDown(tester.view.reset);
      final state = demoWorkspace();
      state.threads['a'] = thread('a');
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
      final field = find.byKey(const Key('composer-input'));
      final editableFinder = find.descendant(
        of: field,
        matching: find.byType(EditableText),
      );
      final editable = tester.state<EditableTextState>(editableFinder);
      Rect bounds() =>
          tester.getRect(find.byKey(const Key('message-composer')));
      final collapsed = bounds();
      expect(tester.widget<TextField>(field).decoration?.hintText, isNull);
      await renderEvidence(tester, 'composer-collapsed');

      await tester.tap(field);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 70));
      final opening = bounds();
      expect(opening.width, greaterThan(collapsed.width));
      expect(opening.width, lessThan(366));
      expect(opening.height, greaterThan(collapsed.height));
      expect(editable.widget.focusNode.hasFocus, true);
      expect(tester.testTextInput.isVisible, true);
      await renderEvidence(tester, 'composer-expanding');

      tester.view.viewInsets = const FakeViewPadding(bottom: 300);
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      final opened = bounds();
      expect(opened.width, closeTo(366, 1));
      expect(opened.bottom, closeTo(844 - 300 - 8, 1));
      expect(tester.state<EditableTextState>(editableFinder), same(editable));
      expect(editable.widget.focusNode.hasFocus, true);
      await renderEvidence(tester, 'composer-focused');

      const draft = '继续完善手机端';
      tester.testTextInput.updateEditingValue(
        const TextEditingValue(
          text: draft,
          selection: TextSelection.collapsed(offset: 3),
          composing: TextRange(start: 0, end: draft.length),
        ),
      );
      state.changed();
      await tester.pump();
      expect(state.draft, draft);
      expect(
        editable.widget.controller.value.composing,
        const TextRange(start: 0, end: draft.length),
      );

      await tester.tapAt(const Offset(200, 160));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 70));
      final closing = bounds();
      expect(closing.width, lessThan(opened.width));
      expect(closing.width, greaterThan(collapsed.width));
      expect(closing.height, lessThan(opened.height));
      expect(editable.widget.focusNode.hasFocus, false);
      await renderEvidence(tester, 'composer-collapsing');

      // A second focus reverses the current morph instead of snapping or
      // replacing the editor with a fresh input connection.
      editable.widget.focusNode.requestFocus();
      await tester.pump();
      expect(bounds().width, closeTo(closing.width, 1));
      await tester.pump(const Duration(milliseconds: 70));
      expect(bounds().width, greaterThan(closing.width));
      expect(tester.state<EditableTextState>(editableFinder), same(editable));
      editable.widget.focusNode.unfocus();
      await tester.pump();

      tester.view.viewInsets = const FakeViewPadding();
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(bounds().width, closeTo(collapsed.width, 1));
      expect(bounds().height, closeTo(collapsed.height, 1));
      expect(editable.widget.controller.text, draft);
      expect(editable.widget.controller.selection.baseOffset, 3);
      expect(tester.state<EditableTextState>(editableFinder), same(editable));
      await renderEvidence(tester, 'composer-restored');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
  testWidgets(
    'transcript groups progress and supports copying code and reusing a prompt',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      tester.view.viewPadding = const FakeViewPadding(top: 31, bottom: 24);
      tester.view.padding = tester.view.viewPadding;
      addTearDown(tester.view.reset);
      const prompt = '整理手机端对话页，给出验证命令。';
      const code = 'git diff --check\nflutter test test/ui_test.dart';
      final now = DateTime.now().millisecondsSinceEpoch;
      final state = demoWorkspace()
        ..threads = {'a': thread('a', status: 'idle', goal: demoGoal())}
        ..messages = [
          {
            ...message(1, role: 'user', text: prompt),
            'createdAtMs': now - 120000,
          },
          {
            ...message(2, text: '先定位滚动入口。'),
            'phase': 'commentary',
            'createdAtMs': now - 110000,
          },
          {
            ...message(3, text: '复核代码块宽度和触控区域。'),
            'phase': 'commentary',
            'createdAtMs': now - 80000,
          },
          {
            ...message(4, text: '布局已调整。用下面的命令重新验证。\n\n```bash\n$code\n```'),
            'createdAtMs': now - 55000,
          },
        ];
      addTearDown(state.dispose);
      String? clipboard;
      final messenger = tester.binding.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          clipboard = (call.arguments as Map)['text'] as String;
        }
        return null;
      });
      addTearDown(
        () => messenger.setMockMethodCallHandler(SystemChannels.platform, null),
      );
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
      await tester.pumpAndSettle();
      expect(find.text('执行过程'), findsOneWidget);
      expect(find.text('2 条'), findsOneWidget);
      expect(find.textContaining('定位滚动入口'), findsNothing);
      expect(find.text('bash'), findsOneWidget);
      expect(find.text('复制代码'), findsOneWidget);
      final titleBottom = tester.getBottomLeft(find.text('设计桌面 AI 对话监控产品')).dy;
      expect(
        tester.getTopLeft(find.text(prompt)).dy,
        lessThan(titleBottom + 100),
      );
      await renderEvidence(tester, 'chat-transcript-native');

      await tester.tap(find.text('复制代码'));
      await tester.pump();
      expect(clipboard, code);
      expect(find.text('已复制'), findsOneWidget);
      await tester.tap(find.text('执行过程'));
      await tester.pumpAndSettle();
      expect(find.text('执行过程 · 2 条'), findsOneWidget);
      expect(find.textContaining('定位滚动入口'), findsOneWidget);
      await renderEvidence(tester, 'chat-progress-native');
      await tester.tap(find.byTooltip('关闭执行过程'));
      await tester.pumpAndSettle();

      state.setDraft('先保留现有草稿。');
      await tester.pump();
      await tester.tap(find.byTooltip('放入输入框'));
      await tester.pumpAndSettle();
      expect(state.draft, '先保留现有草稿。\n\n$prompt');
      final field = tester.widget<EditableText>(find.byType(EditableText).last);
      expect(field.focusNode.hasFocus, true);
      expect(
        (state.api as FakeApi).calls.where((call) => call.method != 'GET'),
        isEmpty,
      );
    },
  );

  testWidgets(
    'incoming updates keep the history scroll position until latest is selected',
    (tester) async {
      tester.view.physicalSize = const Size(390, 844);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final state = demoWorkspace()
        ..messages = objects(page(0, 25)['messages']);
      addTearDown(state.dispose);
      var recent = page(0, 25);
      (state.api as FakeApi).respond = (call) =>
          call.path.contains('/messages?')
          ? recent
          : FakeApi.defaultResponse(call);
      await tester.pumpWidget(
        AIRemoteApp(
          workspace: state,
          updater: null,
          native: NativePlatform(),
          connect: (_) async {},
          changeServer: () {},
        ),
      );
      await tester.pumpAndSettle();
      final transcript = find.byType(ChatTranscript);
      final list = find
          .descendant(of: transcript, matching: find.byType(Scrollable))
          .first;
      await tester.drag(list, const Offset(0, 320));
      await tester.pumpAndSettle();
      final position = tester.state<ScrollableState>(list).position;
      final readingOffset = position.pixels;
      expect(readingOffset, greaterThan(72));
      expect(find.text('回到最新'), findsOneWidget);

      await state.refreshChat();
      await tester.pump();
      expect(find.text('有更新 · 回到最新'), findsNothing);
      recent = {
        ...page(0, 25),
        'messages': [...state.messages, message(26, text: '收到一条新回复')],
      };
      await state.refreshChat();
      await tester.pumpAndSettle();
      expect(position.pixels, closeTo(readingOffset, 1));
      expect(find.text('有更新 · 回到最新'), findsOneWidget);
      expect(find.text('收到一条新回复'), findsNothing);
      await tester.tap(find.text('有更新 · 回到最新'));
      await tester.pumpAndSettle();
      expect(position.pixels, closeTo(0, 1));
      expect(find.text('收到一条新回复'), findsOneWidget);
    },
  );

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
    await tester.tap(find.byKey(const Key('composer-input')));
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 300);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    final composer = tester.getRect(find.byKey(const Key('message-composer')));
    expect(composer.bottom, closeTo(844 - 300 - 8, 1));
    expect(tester.takeException(), isNull);
    await renderEvidence(tester, 'flutter-keyboard');
    // Android can dismiss the keyboard while leaving its TextField focused.
    tester.view.viewInsets = const FakeViewPadding();
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('composer-input')))
          .focusNode!
          .hasFocus,
      false,
    );
    expect(
      tester.getRect(find.byKey(const Key('message-composer'))).width,
      closeTo(322, 1),
    );
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
    await tester.tap(find.byKey(const Key('composer-input')));
    await tester.pump();
    tester.view.viewInsets = const FakeViewPadding(bottom: 280);
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
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
      Future<void> openSettings() async {
        await tester.tap(find.byTooltip('对话选项'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('应用设置与更新'));
      }

      await openSettings();
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
      await openSettings();
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
  testWidgets(
    'compact goal actions edit directly and require close confirmation',
    (tester) async {
      tester.view.physicalSize = const Size(320, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.reset);
      final state = demoWorkspace();
      final goal = (state.threads['a']!['goalState'] as Map)['goal'] as Map;
      goal['status'] = 'paused';
      goal['timeUsedSeconds'] = 1171212;
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
      expect(find.text('已暂停'), findsOneWidget);
      expect(find.byTooltip('继续目标'), findsOneWidget);
      expect(tester.takeException(), isNull);
      await renderEvidence(tester, 'goal-paused-small');
      await tester.tap(find.byTooltip('目标更多操作'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await renderEvidence(tester, 'goal-more-actions');
      await tester.tap(find.text('修改目标'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      expect(find.byType(GoalSheet), findsOneWidget);
      expect(find.byType(TextField), findsNWidgets(3));
      expect(find.text('保存修改'), findsOneWidget);
      await tester.tap(find.byTooltip('关闭目标详情'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.tap(find.byTooltip('目标更多操作'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 400));
      await tester.tap(find.text('关闭目标'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(find.text('关闭这个目标？'), findsOneWidget);
      expect(
        (state.api as FakeApi).calls.where((call) => call.method != 'GET'),
        isEmpty,
      );
      await tester.tap(find.text('保留目标'));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 300));
      expect(state.goal, equals(goal));
      goal['status'] = 'complete';
      state.changed();
      await tester.pump();
      expect(find.text('已完成'), findsOneWidget);
      expect(find.byTooltip('继续目标'), findsNothing);
      expect(find.byTooltip('暂停目标'), findsNothing);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}
