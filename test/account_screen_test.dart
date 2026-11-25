import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/platform.dart';
import 'package:airemote_mobile/core/updates.dart';
import 'package:airemote_mobile/core/workspace.dart';
import 'package:airemote_mobile/ui/app.dart';
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'support.dart';

const mac = {
  'id': 'mac',
  'account': 'owner',
  'name': '书房的 Mac',
  'platform': 'macos',
  'online': true,
  'connectorOnline': true,
};
const windows = {
  'id': 'win-pc',
  'account': 'member',
  'name': '工作电脑',
  'platform': 'windows',
  'online': false,
};
num expiresSoon() => DateTime.now().millisecondsSinceEpoch / 1000 + 600;

Future<void> loadApp(WidgetTester tester, Workspace state) async {
  tester.view.physicalSize = const Size(390, 844);
  tester.view.devicePixelRatio = 1;
  addTearDown(tester.view.reset);
  addTearDown(state.dispose);
  final updater = AppUpdater(MemoryVault());
  addTearDown(updater.dispose);
  await tester.pumpWidget(
    AIRemoteApp(
      workspace: state,
      updater: updater,
      native: NativePlatform(),
      connect: (_) async {},
      changeServer: () {},
    ),
  );
  await tester.pumpAndSettle();
}

Future<void> openAccount(WidgetTester tester) async {
  final chatActions = find.byTooltip('对话选项');
  if (chatActions.evaluate().isEmpty) {
    await tester.tap(find.byTooltip('应用设置与更新'));
  } else {
    await tester.tap(chatActions);
    await tester.pumpAndSettle();
    await tester.tap(find.text('应用设置与更新'));
  }
  await tester.pumpAndSettle();
  await tester.tap(find.text('账号与设备'));
  await tester.pumpAndSettle();
  expect(find.widgetWithText(AppBar, '账号与设备'), findsOneWidget);
}

void main() {
  testWidgets(
    'account opens inside the app, shows devices, copies a code, and returns to the same chat',
    (tester) async {
      final state = demoWorkspace()
        ..threads = {'a': thread('a', status: 'idle')}
        ..setDraft('保留的草稿');
      final api = state.api as FakeApi;
      var unpaired = false;
      api.respondApi = (request) => switch (request.path) {
        '/api/account' => {
          'username': 'member',
          'isAdmin': false,
          'devices': [mac, if (!unpaired) windows],
        },
        '/api/account/pairings' => {
          'code': 'test-pair-code',
          'expiresAt': expiresSoon(),
        },
        '/api/account/devices/win-pc/revoke' => {'ok': unpaired = true},
        _ => {'online': true, 'connectorOnline': true},
      };
      String? clipboard;
      final messenger = tester.binding.defaultBinaryMessenger;
      messenger.setMockMethodCallHandler(SystemChannels.platform, (call) async {
        if (call.method == 'Clipboard.setData') {
          clipboard = call.arguments['text'] as String;
        }
        return null;
      });
      addTearDown(
        () => messenger.setMockMethodCallHandler(SystemChannels.platform, null),
      );
      await loadApp(tester, state);
      expect(api.apiCalls, isEmpty);
      await openAccount(tester);
      expect(find.text('Mac · 已连接'), findsOneWidget);
      expect(find.text('Windows · 离线'), findsOneWidget);
      expect(find.text('管理员'), findsNothing);
      await tester.tap(find.text('生成配对码'));
      await tester.pumpAndSettle();
      expect(find.text('test-pair-code'), findsOneWidget);
      expect(
        api.apiCalls
            .where((call) => call.path.endsWith('/pairings'))
            .single
            .method,
        'POST',
      );
      await tester.tap(find.text('复制'));
      await tester.pumpAndSettle();
      expect(clipboard, 'test-pair-code');

      final remove = find.descendant(
        of: find.widgetWithText(ListTile, '工作电脑'),
        matching: find.text('解绑'),
      );
      await tester.ensureVisible(remove);
      await tester.tap(remove);
      await tester.pumpAndSettle();
      expect(unpaired, isFalse);
      await tester.tap(
        find.descendant(
          of: find.byType(AlertDialog),
          matching: find.text('解绑'),
        ),
      );
      await tester.pumpAndSettle();
      expect(unpaired, isTrue);
      expect(find.text('工作电脑'), findsNothing);
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.byTooltip('对话选项'), findsOneWidget);
      expect(state.selected, 'a');
      expect(state.draft, '保留的草稿');
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets(
    'administrator manages invitations and account status in the app',
    (tester) async {
      final api = FakeApi();
      final state = Workspace(api)
        ..authenticated = true
        ..booting = false;
      var enabled = true;
      Json? invitation;
      api.respondApi = (request) {
        switch (request.path) {
          case '/api/account':
            return {
              'username': 'admin',
              'isAdmin': true,
              'devices': [mac],
            };
          case '/api/admin':
            return {
              'accounts': [
                {'id': 'owner', 'username': 'admin', 'enabled': 1},
                {'id': 'member', 'username': 'lin', 'enabled': enabled ? 1 : 0},
              ],
              'devices': [mac, windows],
              'invitations': [?invitation],
            };
          case '/api/admin/invitations':
            return invitation = {
              'id': 'invite',
              'username': request.body['username'],
              'code': 'test-invite-code',
              'expiresAt': expiresSoon(),
            };
          case '/api/admin/invitations/invite/revoke':
            invitation = null;
            return {'ok': true};
          case '/api/admin/accounts/member/enabled':
            enabled = request.body['enabled'] == true;
            return {'ok': true};
          default:
            throw StateError('Unexpected request ${request.path}');
        }
      };
      await loadApp(tester, state);
      await openAccount(tester);
      await tester.tap(find.text('管理员'));
      await tester.pumpAndSettle();
      expect(find.widgetWithText(AppBar, '管理员'), findsOneWidget);
      await tester.enterText(find.byType(TextField), ' invited-user ');
      await tester.tap(find.text('生成邀请码'));
      await tester.pumpAndSettle();
      expect(find.text('test-invite-code'), findsOneWidget);
      expect(invitation!['username'], 'invited-user');
      await tester.ensureVisible(find.text('撤销'));
      await tester.tap(find.text('撤销'));
      await tester.pumpAndSettle();
      expect(invitation, isNull);
      expect(find.text('test-invite-code'), findsNothing);
      await tester.ensureVisible(find.text('停用账号'));
      await tester.tap(find.text('停用账号'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('停用'));
      await tester.pumpAndSettle();
      expect(enabled, isFalse);
      expect(find.text('启用账号'), findsOneWidget);
      await tester.binding.handlePopRoute();
      await tester.pumpAndSettle();
      expect(find.widgetWithText(AppBar, '账号与设备'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );

  testWidgets('expired session clears the visible device data', (tester) async {
    final api = FakeApi();
    final state = Workspace(api)
      ..authenticated = true
      ..booting = false;
    var expired = false;
    api.respondApi = (_) {
      if (expired) throw const ApiError('登录已过期', 401);
      return {
        'username': 'member',
        'devices': [windows],
      };
    };
    await loadApp(tester, state);
    await openAccount(tester);
    expect(find.text('工作电脑'), findsOneWidget);
    expired = true;
    await tester.tap(find.byTooltip('刷新'));
    await tester.pumpAndSettle();
    expect(find.text('工作电脑'), findsNothing);
    expect(state.authenticated, isFalse);
    await tester.tap(find.text('登录已过期，返回登录'));
    await tester.pumpAndSettle();
    expect(find.text('登录'), findsOneWidget);
    await tester.pumpWidget(const SizedBox.shrink());
  });

  testWidgets(
    'an invitation registers and signs in without opening a browser',
    (tester) async {
      final api = FakeApi();
      final state = Workspace(api)..booting = false;
      await loadApp(tester, state);
      await tester.tap(find.text('使用邀请码'));
      await tester.pumpAndSettle();
      await tester.enterText(
        find.widgetWithText(TextField, '邀请码'),
        ' test-invite-code ',
      );
      await tester.enterText(
        find.widgetWithText(TextField, '设置密码'),
        'test-password-1234',
      );
      await tester.tap(find.text('注册并登录'));
      await tester.pumpAndSettle();
      final registration = api.apiCalls.single;
      expect(registration.path, '/api/register');
      expect(registration.body['code'], 'test-invite-code');
      expect(registration.body['password'], 'test-password-1234');
      expect(state.authenticated, isTrue);
      expect(find.byTooltip('应用设置与更新'), findsOneWidget);
      await tester.pumpWidget(const SizedBox.shrink());
    },
  );
}
