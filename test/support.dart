import 'dart:async';
import 'package:airemote_mobile/core/api.dart';
import 'package:airemote_mobile/core/workspace.dart';

class MemoryVault implements Vault {
  final values = <String, String>{};
  @override
  Future<String?> read(String key) async => values[key];
  @override
  Future<void> remove(String key) async {
    values.remove(key);
  }

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }
}

class Call {
  final String path, method;
  final Json body;
  final String? key;
  const Call(this.path, this.method, this.body, this.key);
}

class FakeApi implements RemoteApi {
  final calls = <Call>[];
  FutureOr<Json> Function(Call)? respond;
  @override
  String get server => 'https://example.com';
  @override
  String get cookieHeader => '';
  @override
  Future<Json> session() async => {
    'device': {'online': true, 'connectorOnline': true},
  };
  @override
  Future<Json> login(String username, String password) => session();
  @override
  Future<void> logout() async {}
  @override
  Future<Json> api(
    String path, {
    String method = 'GET',
    Json body = const {},
  }) async => {'online': true, 'connectorOnline': true};
  @override
  Future<Json> bridge(
    String path, {
    String method = 'GET',
    Json body = const {},
    String? key,
  }) async {
    final call = Call(path, method, body, key);
    calls.add(call);
    if (respond != null) return await respond!(call);
    return defaultResponse(call);
  }

  static Json defaultResponse(Call call) {
    if (call.path == '/api/providers') {
      return {
        'providers': [
          for (final id in ['codex', 'deepseek'])
            {
              'id': id,
              'connected': true,
              'readable': true,
              'features': ['send', 'queue-reorder'],
            },
        ],
      };
    }
    if (call.path.endsWith('/projects')) {
      return {
        'projects': [
          {
            'id': 'p',
            'name': 'AIRemote',
            'path': '/projects/AIRemote',
            'roots': ['/projects/AIRemote'],
          },
        ],
      };
    }
    if (call.path.endsWith('/threads') && call.method == 'GET') {
      return {
        'threads': [thread('a')],
      };
    }
    if (call.path.contains('/messages?')) return page(40, 79);
    if (call.path.endsWith('/queue')) return {'items': []};
    return thread(call.path.split('/').last);
  }

  @override
  Future<Uri> artifactUrl(String path) async {
    calls.add(Call(path, 'GET', {}, null));
    return Uri.parse(
      '$server/api/requests/00000000-0000-0000-0000-000000000000/result',
    );
  }

  @override
  void close() {}
}

Json thread(String id, {String status = 'active', Json? goal}) => {
  'threadId': id,
  'title': '设计桌面 AI 对话监控产品',
  'cwd': '/projects/AIRemote',
  'runtimeStatus': {'type': status},
  'turns': [
    {
      'turn_id': 'turn',
      'status': 'completed',
      'started_at': 1700000000,
      'completed_at': 1700000065,
    },
  ],
  'modelSettings': {
    'model': 'gpt-6.1-sol',
    'reasoningEffort': 'max',
    'collaborationMode': 'default',
    'permissionMode': 'dangerFullAccess',
  },
  'goalState': {
    'goal': goal,
    'readable': true,
    'writable': true,
    'revision': 'revision-1',
  },
};
Json message(int n, {String? text, String role = 'assistant'}) => {
  'itemId': 'm$n',
  'ordinal': n,
  'turnId': 'turn',
  'role': role,
  'text': text ?? '消息 $n',
  'phase': 'final_answer',
  'createdAtMs': 1700000000000 + n * 1000,
};
Json page(int start, int end) => {
  'messages': [for (var n = start; n <= end; n++) message(n)],
  'hasMore': start > 0,
  'olderCursor': start > 0 ? 'before-$start' : null,
};
Json demoGoal() => {
  'threadId': 'a',
  'objective': '完成 AIRemote 手机客户端，验证消息同步和原生队列',
  'status': 'active',
  'tokenBudget': 6000,
  'tokensUsed': 1034,
  'timeUsedSeconds': 1201,
};
Workspace demoWorkspace() {
  final value = Workspace(FakeApi())
    ..authenticated = true
    ..booting = false
    ..device = {'online': true, 'connectorOnline': true}
    ..providersById = {
      'codex': {
        'readable': true,
        'connected': true,
        'features': ['send', 'queue-reorder'],
      },
      'deepseek': {'readable': false, 'connected': false},
    }
    ..selected = 'a'
    ..projects = [
      {
        'id': 'p',
        'name': 'AIRemote',
        'roots': ['/projects/AIRemote'],
        'path': '/projects/AIRemote',
      },
      {'id': 'p2', 'name': 'OnlineGameForWeb'},
      {'id': 'p3', 'name': 'Royal Smash'},
    ]
    ..threads = {'a': thread('a', goal: demoGoal())}
    ..messages = [
      message(1, role: 'user', text: '继续完善手机端，目标状态放在输入框上方，侧栏铺满整个屏幕。'),
      message(2, text: '已接上现有连接器。\n\n- 项目与对话按需读取\n- 同一对话继续发送指令\n- 目标与原生队列即时同步'),
    ]
    ..queue = [];
  value.expandedProjects.add('p');
  value.projectChats['p'] = [
    thread('a'),
    {...thread('b', status: 'idle'), 'title': 'AIRemote 手机客户端验证'},
  ];
  return value;
}
