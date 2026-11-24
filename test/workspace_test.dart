import 'dart:async';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/workspace.dart';
import 'support.dart';

class EventApi extends FakeApi implements RelayEvents {
  final subscriptions = <(String, String, String)>[];
  final streams = <StreamController<RelayEvent>>[];

  @override
  Future<Json> session() async => {
    ...await super.session(),
    'eventStream': true,
  };

  @override
  Stream<RelayEvent> watchEvents(
    String provider,
    String thread,
    String cursor,
  ) {
    subscriptions.add((provider, thread, cursor));
    final stream = StreamController<RelayEvent>();
    streams.add(stream);
    return stream.stream;
  }

  @override
  void close() {
    for (final stream in streams) {
      unawaited(stream.close());
    }
  }
}

void main() {
  testWidgets(
    'foreground reconnect keeps the cursor and refreshes only the selected chat',
    (tester) async {
      final api = EventApi();
      final workspace = Workspace(api);
      addTearDown(workspace.dispose);
      await workspace.start();
      await workspace.select('a');
      expect(api.subscriptions.last, ('codex', 'a', ''));
      api.streams.last.add(
        const RelayEvent('epoch:12', 'chat', {
          'provider': 'codex',
          'threadId': 'a',
        }),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(
        api.calls.where(
          (call) => call.path == '/api/threads/a/messages?limit=40',
        ),
        hasLength(2),
      );

      workspace.setVisible(false);
      expect(api.streams.last.hasListener, isFalse);
      workspace.setVisible(true);
      expect(api.subscriptions.last, ('codex', 'a', 'epoch:12'));
      await tester.pump();
      api.calls.clear();
      api.streams.last.add(
        const RelayEvent('epoch:13', 'chat', {
          'provider': 'codex',
          'threadId': 'b',
        }),
      );
      api.streams.last.add(
        const RelayEvent('epoch:14', 'chat', {
          'provider': 'claude',
          'threadId': 'a',
        }),
      );
      api.streams.last.add(
        const RelayEvent('epoch:15', 'chat', {
          'provider': 'codex',
          'threadId': 'a',
        }),
      );
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
      expect(
        api.calls
            .where((c) => c.path.contains('/messages?'))
            .map((c) => c.path),
        ['/api/threads/a/messages?limit=40'],
      );
    },
  );

  test(
    'initial load fetches only projects; expansion and selection are lazy',
    () async {
      final api = FakeApi();
      final w = Workspace(api);
      addTearDown(w.dispose);
      await w.start();
      expect(api.calls.map((c) => c.path), ['/api/providers', '/api/projects']);
      await w.loadProject('p');
      expect(api.calls.last.path, '/api/projects/p/threads');
      await w.select('a');
      expect(api.calls.skip(3).map((c) => c.path).toSet(), {
        '/api/threads/a',
        '/api/threads/a/messages?limit=40',
        '/api/threads/a/queue',
      });
      expect(api.calls.where((c) => c.path.contains('before=')), isEmpty);
      await w.loadOlder();
      expect(api.calls.last.path, contains('before=before-40'));
    },
  );
  test(
    'Claude reads scoped history without a queue and respects native write availability',
    () async {
      final api = FakeApi();
      final state = Workspace(api);
      addTearDown(state.dispose);
      const root = '/api/providers/claude';
      var writable = false;
      api.respond = (call) {
        if (call.path == '/api/providers') {
          return {
            'providers': [
              ...FakeApi.defaultResponse(call)['providers'] as List,
              {
                'id': 'claude',
                'readable': true,
                'connected': true,
                'features': ['send', 'create'],
              },
            ],
          };
        }
        if (call.path == '$root/threads/a') {
          return {...thread('a', status: 'idle'), 'writeAvailable': writable};
        }
        if (call.path == '$root/threads/a/messages' && call.method == 'POST' ||
            call.path == '$root/operations/native-claude-operation') {
          return {
            'operationId': 'native-claude-operation',
            'requestStatus': 'in_flight',
            'kind': 'send',
            'result': {'threadId': 'a', 'executionState': 'starting'},
          };
        }
        return FakeApi.defaultResponse(call);
      };
      await state.start();
      await state.switchProvider('claude');
      await state.loadProject('p');
      await state.select('a');
      expect(state.online, isTrue);
      expect(state.messages.last['itemId'], 'm79');
      expect(state.canWrite, isFalse);
      state.setDraft('继续');
      expect(await state.send(), isFalse);
      expect(state.draft, '继续');
      writable = true;
      await state.refreshChat();
      expect(await state.send(), isTrue);
      expect(state.receipt['requestStatus'], 'in_flight');
      final writes = api.calls.where((call) => call.method == 'POST');
      expect(writes.single.path, '$root/threads/a/messages');
      expect(api.calls.where((call) => call.path.endsWith('/queue')), isEmpty);
    },
  );
  test(
    'Claude can queue during an owned turn and sends settings and stop to that session',
    () async {
      final state = demoWorkspace()..provider = 'claude';
      addTearDown(state.dispose);
      const root = '/api/providers/claude/threads/a';
      state.providersById['claude'] = {
        'readable': true,
        'connected': true,
        'features': ['queue', 'send', 'stop'],
      };
      final active = {
        ...thread('a'),
        'writeAvailable': false,
        'queueAvailable': true,
        'executionId': 'owned-run',
        'stopAvailable': true,
        'claudeSettings': {'writable': true},
      };
      state.threads['a'] = active;
      final api = state.api as FakeApi;
      api.respond = (call) {
        if (call.path == root) return active;
        if (call.path == '$root/queue' && call.method == 'POST' ||
            call.path.endsWith('/operations/pending-claude')) {
          return {
            'operationId': 'pending-claude',
            'kind': 'queue_add',
            'requestStatus': 'queued',
            'result': {'threadId': 'a'},
          };
        }
        if (call.path == '$root/stop') {
          return {'kind': 'stop', 'requestStatus': 'applied'};
        }
        if (call.path == '$root/settings') {
          return {'kind': 'settings', 'requestStatus': 'applied'};
        }
        return FakeApi.defaultResponse(call);
      };
      expect(state.canWrite, isFalse);
      expect(state.effectiveSendMode, 'queue');
      state.setDraft('下一条指令');
      expect(await state.send(), isTrue);
      expect(state.receipt['requestStatus'], 'queued');
      expect(
        await state.saveClaudeSettings({
          'model': 'haiku',
          'effort': 'low',
          'mode': 'plan',
        }),
        isTrue,
      );
      await state.stopClaude();
      final mutations = api.calls
          .where((call) => call.method != 'GET')
          .toList();
      expect(mutations.map((call) => call.path), [
        '$root/queue',
        '$root/settings',
        '$root/stop',
      ]);
      expect(mutations.last.body['executionId'], 'owned-run');
      expect(state.receipt['operationId'], 'pending-claude');
    },
  );
  test(
    'slow queue does not hold back messages; A B A discards stale replies',
    () async {
      final api = FakeApi();
      final w = Workspace(api);
      addTearDown(w.dispose);
      await w.start();
      final oldMessages = Completer<Json>(), slowQueue = Completer<Json>();
      var aReads = 0;
      api.respond = (c) {
        if (c.path == '/api/threads/a/messages?limit=40' && aReads++ == 0) {
          return oldMessages.future;
        }
        if (c.path == '/api/threads/a/queue' && !slowQueue.isCompleted) {
          return slowQueue.future;
        }
        return FakeApi.defaultResponse(c);
      };
      final first = w.select('a');
      await Future<void>.delayed(Duration.zero);
      await w.select('b');
      final latest = w.select('a');
      await Future<void>.delayed(Duration.zero);
      expect(w.messages.last['itemId'], 'm79');
      expect(w.loading, false);
      oldMessages.complete(page(0, 1));
      slowQueue.complete({'items': []});
      await Future.wait([first, latest]);
      expect(w.messages.last['itemId'], 'm79');
    },
  );
  test(
    'history cursor survives overlapping refresh; disjoint page waits while reading',
    () async {
      final api = FakeApi();
      final w = Workspace(api);
      addTearDown(w.dispose);
      await w.start();
      await w.select('a');
      api.respond = (c) => c.path.contains('before=')
          ? page(0, 39)
          : c.path.contains('/messages?')
          ? page(42, 81)
          : FakeApi.defaultResponse(c);
      await w.loadOlder();
      expect(w.messages.first['itemId'], 'm0');
      expect(w.hasOlder, false);
      await w.refreshChat();
      expect(w.messages.first['itemId'], 'm0');
      expect(w.messages.last['itemId'], 'm81');
      api.respond = (c) => c.path.contains('/messages?')
          ? page(100, 139)
          : FakeApi.defaultResponse(c);
      await w.refreshChat();
      expect(w.messages.last['itemId'], 'm81');
      expect(w.hasNewMessages, true);
      w.jumpLatest();
      expect(w.messages.first['itemId'], 'm100');
      expect(w.olderCursor, 'before-100');
    },
  );
  test(
    'uncertain mutation retries same key and keeps unsent draft; new writes get fresh keys',
    () async {
      final api = FakeApi();
      final w = Workspace(api);
      addTearDown(w.dispose);
      await w.start();
      await w.select('a');
      w.setDraft('下一步');
      final keys = <String?>[];
      api.respond = (c) {
        if (c.method != 'GET') {
          keys.add(c.key);
          if (keys.length == 1) throw const ApiError('网络中断', 504, true);
          return {
            'operationId': 'op',
            'result': {'threadId': 'a'},
          };
        }
        return FakeApi.defaultResponse(c);
      };
      expect(await w.send(), false);
      expect(w.draft, '下一步');
      expect(await w.send(), true);
      expect(w.draft, '');
      expect(keys[0], keys[1]);
      w.setDraft('下一步');
      await w.send();
      expect(keys.last, isNot(keys.first));
    },
  );
  test(
    'goal receipt updates immediately without waiting for queue and stale status cannot undo it',
    () async {
      final api = FakeApi();

      final state = Workspace(api);
      addTearDown(state.dispose);
      await state.start();
      await state.select('a');
      final pendingStatus = Completer<Json>(), pendingQueue = Completer<Json>();
      api.respond = (c) {
        if (c.method == 'PATCH') {
          return {
            'requestStatus': 'applied',
            'result': {
              'threadId': 'a',
              'acknowledged': true,
              'nativeConfirmed': true,
              'goal': {...demoGoal(), 'status': 'paused'},
              'revision': 'revision-2',
            },
          };
        }
        if (c.path == '/api/threads/a') return pendingStatus.future;
        if (c.path.endsWith('/queue')) return pendingQueue.future;
        return FakeApi.defaultResponse(c);
      };
      final staleRead = state.refreshChat();
      await Future<void>.delayed(Duration.zero);
      expect(await state.goalAction('PATCH', {'status': 'paused'}), true);
      expect(state.goal?['status'], 'paused');
      expect(state.goalState?['revision'], 'revision-2');
      pendingStatus.complete(thread('a', goal: demoGoal()));
      pendingQueue.complete({'items': []});
      await staleRead;
      expect(state.goal?['status'], 'paused');
    },
  );
  test(
    'cached active status never appears live while offline or status reads fail',
    () {
      final state = demoWorkspace();
      addTearDown(state.dispose);
      expect(state.isRunning, true);
      state.device = {'online': false, 'connectorOnline': false};
      expect(state.isRunning, false);
      state.device = {'online': true, 'connectorOnline': true};
      state.laneErrors['thread'] = 'timeout';
      expect(state.isRunning, false);
    },
  );
  test('turn metadata keeps terminal status and seconds across pagination', () {
    final merged = mergeTurns(
      [
        {
          'turn_id': 'a',
          'status': 'completed',
          'started_at': 1,
          'completed_at': 66,
        },
      ],
      [
        {'turn_id': 'a', 'status': 'inProgress'},
      ],
    );
    expect(merged.single['completed_at'], 66);
    expect(durationLabel(65), '1m 5s');
  });
}
