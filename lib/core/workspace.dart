import 'dart:async';
import 'dart:convert';
import 'package:flutter/foundation.dart';
import 'api.dart';

const providerNames = {
  'codex': 'Codex',
  'deepseek': 'DeepSeek',
  'claude': 'Claude',
};

String providerPath(String provider, String path) =>
    provider == 'codex' ? path : '/api/providers/$provider${path.substring(4)}';
String queueText(Json item) =>
    objects(item['input']).map((p) => p['text'] ?? '').join('\n');
String runtimeType(Json? chat) => chat?['runtimeStatus'] is String
    ? chat!['runtimeStatus']
    : (chat?['runtimeStatus'] is Map
          ? chat!['runtimeStatus']['type'] ?? ''
          : '');
String runtimeLabel(Json? chat) {
  final flags = chat?['runtimeStatus'] is Map
      ? chat!['runtimeStatus']['activeFlags'] as List? ?? []
      : [];
  if (flags.any(
    (f) =>
        RegExp('approval|input|attention', caseSensitive: false).hasMatch('$f'),
  )) {
    return '等待处理';
  }
  return switch (runtimeType(chat)) {
    'active' => '运行中',
    'idle' => '空闲',
    'notLoaded' => '未加载',
    _ => '状态待同步',
  };
}

String goalLabel(String? status) => switch (status) {
  'active' => '进行中的目标',
  'paused' => '已暂停的目标',
  'blocked' => '受阻的目标',
  'budgetLimited' => '预算已用完',
  'usageLimited' => '用量受限',
  'complete' => '已完成的目标',
  _ => '对话目标',
};
String durationLabel(num? value) {
  final seconds = (value ?? 0).floor().clamp(0, 1 << 50);
  final days = seconds ~/ 86400,
      hours = seconds % 86400 ~/ 3600,
      minutes = seconds % 3600 ~/ 60;
  return '${days > 0 ? '${days}d ' : ''}${days > 0 || hours > 0 ? '${hours}h ' : ''}${days > 0 || hours > 0 || minutes > 0 ? '${minutes}m ' : ''}${seconds % 60}s';
}

String userText(String text) {
  const marker = '## My request:';
  if (text.trimLeft().startsWith('# Files mentioned by the user:') &&
      text.contains(marker)) {
    return text.substring(text.indexOf(marker) + marker.length).trim();
  }
  final match = RegExp(
    r'^\s*<send_user_message_question_reply>\s*([\s\S]*?)\s*</send_user_message_question_reply>\s*$',
  ).firstMatch(text);
  if (match != null) {
    try {
      return objects(
        jsonDecode(match.group(1)!),
      ).map((v) => v['answer']).join('\n\n');
    } catch (_) {
      /* keep original */
    }
  }
  return text;
}

int compareMessage(Json a, Json b) {
  final order = ((a['ordinal'] as num?) ?? 0).compareTo(
    (b['ordinal'] as num?) ?? 0,
  );
  return order != 0 ? order : '${a['itemId']}'.compareTo('${b['itemId']}');
}

JsonList mergeTurns(dynamic existing, dynamic incoming) {
  const terminal = {'completed', 'failed', 'interrupted'};
  final merged = {for (final turn in objects(existing)) turn['turn_id']: turn};
  for (final turn in objects(incoming)) {
    final old = merged[turn['turn_id']];
    if (old != null &&
        terminal.contains(old['status']) &&
        !terminal.contains(turn['status'])) {
      continue;
    }
    merged[turn['turn_id']] = turn;
  }
  return merged.values.toList()..sort(
    (a, b) => ((b['started_at'] as num?) ?? 0).compareTo(
      (a['started_at'] as num?) ?? 0,
    ),
  );
}

class Workspace extends ChangeNotifier {
  final RemoteApi api;
  bool authenticated = false,
      booting = true,
      disposed = false,
      visible = true,
      drawerOpen = false;
  bool busy = false,
      loading = false,
      loadingOlder = false,
      hasOlder = false,
      followLatest = true,
      hasNewMessages = false;
  String provider = 'codex',
      selected = '',
      error = '',
      notice = '',
      search = '',
      sendMode = 'send';
  Json device = {}, providersById = {}, threads = {}, receipt = {};
  JsonList projects = [], messages = [], queue = [];
  final Set<String> expandedProjects = {}, loadingProjects = {};
  final Map<String, JsonList> projectChats = {};
  final Map<String, String> projectErrors = {},
      laneErrors = {},
      drafts = {},
      _uncertainWrites = {};
  final Set<String> _pending = {};
  String? olderCursor;
  Json? _pendingPage;
  bool _projectsLoaded = false;
  int _generation = 0, _providerGeneration = 0, _goalEpoch = 0;
  Timer? _conversationTimer, _metadataTimer, _projectTimer;
  Workspace(this.api);
  Json? get chat => threads[selected] is Map
      ? Map<String, dynamic>.from(threads[selected])
      : null;
  Json? get goalState => chat?['goalState'] is Map
      ? Map<String, dynamic>.from(chat!['goalState'])
      : null;
  Json? get goal => goalState?['goal'] is Map
      ? Map<String, dynamic>.from(goalState!['goal'])
      : null;
  Json get descriptor => providersById[provider] is Map
      ? Map<String, dynamic>.from(providersById[provider])
      : {};
  bool get online =>
      device['online'] == true &&
      device['connectorOnline'] == true &&
      descriptor['readable'] == true;
  bool get _canOperate => online && descriptor['connected'] == true && !busy;
  bool supports(String feature) =>
      (descriptor['features'] as List? ?? []).contains(feature);
  bool get supportsQueue =>
      chat?['queueAvailable'] != false &&
      (provider != 'claude' ||
          supports('queue') && chat?['queueAvailable'] == true);
  bool get canManageQueue =>
      provider == 'claude' ? _canOperate && supportsQueue : canWrite;
  String get effectiveSendMode => !supportsQueue
      ? 'send'
      : provider == 'claude' && isRunning
      ? 'queue'
      : sendMode;
  bool get canSendDraft =>
      effectiveSendMode == 'queue' ? canManageQueue : canWrite;
  bool get canStopClaude =>
      _canOperate && provider == 'claude' && chat?['stopAvailable'] == true;
  bool get canSetGoal =>
      provider == 'codex' && chat?['writeAvailable'] != false ||
      provider == 'claude' && (goalState?['writable'] == true || goal != null);
  bool get canWrite =>
      _canOperate &&
      chat?['writeAvailable'] != false &&
      supports('send') &&
      (provider != 'claude' ||
          chat?['writeAvailable'] == true &&
              receipt['requestStatus'] != 'in_flight');
  bool get canCreate =>
      _canOperate &&
      (provider == 'claude' ? supports('create') : supports('send'));
  List<String> get createPaths => projects
      .where(
        (project) => provider == 'claude'
            ? project['writable'] == true
            : project['writable'] != false,
      )
      .expand(
        (project) => (project['roots'] as List? ?? []).whereType<String>(),
      )
      .toSet()
      .toList();
  bool get hasLiveStatus =>
      online &&
      descriptor['connected'] == true &&
      !laneErrors.containsKey('thread');
  bool get isRunning => hasLiveStatus && runtimeType(chat) == 'active';
  String get providerName => providerNames[provider] ?? provider;
  String get draftKey => '$provider/$selected';
  String get draft => drafts[draftKey] ?? '';
  void setDraft(String value) {
    drafts[draftKey] = value;
    changed();
  }

  void changed() {
    if (!disposed) notifyListeners();
  }

  Future<Json> read(String path) => api.bridge(providerPath(provider, path));

  Future<void> start() async {
    try {
      final value = await api.session();
      authenticated = true;
      device = Map<String, dynamic>.from(value['device'] ?? {});
    } catch (e) {
      if (e is! ApiError || e.status != 401) error = '$e';
    }
    booting = false;
    changed();
    if (authenticated) {
      try {
        await refreshProviders();
        await loadProjects();
      } catch (e) {
        _error(e);
      }
    }
    if (disposed) return;
    _conversationTimer = Timer.periodic(const Duration(milliseconds: 2500), (
      _,
    ) {
      if (visible && !drawerOpen && online && selected.isNotEmpty) {
        refreshChat();
      }
    });
    _metadataTimer = Timer.periodic(const Duration(seconds: 5), (_) {
      if (visible && authenticated) refreshDevice();
    });
    _projectTimer = Timer.periodic(const Duration(seconds: 6), (_) {
      if (visible && drawerOpen && online) {
        for (final id in expandedProjects.toList()) {
          loadProject(id);
        }
      }
    });
  }

  Future<void> login(String username, String password) =>
      _signIn(() => api.login(username, password));

  Future<void> register(String code, String password) =>
      _signIn(() => api.register(code, password));

  Future<void> _signIn(Future<Json> Function() authenticate) async {
    busy = true;
    error = '';
    changed();
    try {
      final value = await authenticate();
      _clearAccount();
      device = Map<String, dynamic>.from(value['device'] ?? {});
      authenticated = true;
      await refreshProviders();
      await loadProjects();
    } catch (e) {
      error = '$e';
    } finally {
      busy = false;
      changed();
    }
  }

  Future<Json> accountRequest(
    String path, {
    String method = 'GET',
    Json body = const {},
  }) async {
    if (!authenticated || disposed) throw const ApiError('请先登录。', 401);
    try {
      return await api.api(path, method: method, body: body);
    } on ApiError catch (failure) {
      if (failure.status == 401 && !disposed) _error(failure);
      rethrow;
    }
  }

  Future<void> logout() async {
    try {
      await api.logout();
      _clearAccount();
      error = '';
      changed();
    } catch (e) {
      error = '$e';
      changed();
    }
  }

  void _clearAccount() {
    authenticated = false;
    selected = '';
    drawerOpen = false;
    _generation++;
    _providerGeneration++;
    _projectsLoaded = false;
    device = {};
    providersById = {};
    messages = [];
    projects = [];
    projectChats.clear();
    expandedProjects.clear();
    loadingProjects.clear();
    projectErrors.clear();
    laneErrors.clear();
    threads = {};
    queue = [];
    receipt = {};
    drafts.clear();
    _uncertainWrites.clear();
    olderCursor = null;
    hasOlder = false;
    _pendingPage = null;
  }

  Future<void> refreshDevice() async {
    final scope = _providerGeneration, key = 'device:$_providerGeneration';
    if (_pending.contains(key)) return;
    _pending.add(key);
    try {
      final current = await api.api('/api/device');
      if (scope != _providerGeneration || disposed) return;
      if (current['catalogRevision'] != device['catalogRevision']) {
        _projectsLoaded = false;
      }
      device = current;
      await refreshProviders();
      if (online && !_projectsLoaded) await loadProjects();
    } catch (e) {
      if (scope == _providerGeneration) _error(e);
    } finally {
      _pending.remove(key);
      changed();
    }
  }

  Future<void> refreshProviders() async {
    if (device['online'] != true) return;
    final scope = _providerGeneration;
    final value = await api.bridge('/api/providers');
    if (scope != _providerGeneration || disposed) return;
    providersById = {
      for (final item in objects(value['providers']))
        item['id'] as String: item,
    };
    changed();
  }

  Future<void> loadProjects() async {
    final scope = _providerGeneration, chosen = provider;
    final pendingKey = 'projects:$scope:$chosen';
    if (!online || _pending.contains(pendingKey)) return;
    _pending.add(pendingKey);
    changed();
    try {
      final value = await read('/api/projects');
      if (!disposed && chosen == provider && scope == _providerGeneration) {
        projects = objects(value['projects']);
        _projectsLoaded = true;
      }
    } catch (e) {
      if (scope == _providerGeneration) _error(e);
    } finally {
      _pending.remove(pendingKey);
      changed();
    }
  }

  void toggleProject(String id) {
    if (expandedProjects.contains(id)) {
      expandedProjects.remove(id);
    } else {
      expandedProjects.add(id);
      loadProject(id);
    }
    changed();
  }

  Future<void> loadProject(String id) async {
    final scope = _providerGeneration;
    if (!online || loadingProjects.contains(id)) return;
    loadingProjects.add(id);
    projectErrors.remove(id);
    changed();
    try {
      final value = await read('/api/projects/$id/threads');
      if (scope != _providerGeneration || disposed) return;
      final chats = objects(value['threads']);
      projectChats[id] = chats;
      for (final item in chats) {
        if (item['threadId'] != selected || threads[selected] == null) {
          threads[item['threadId']] = item;
        }
      }
    } catch (e) {
      if (scope == _providerGeneration) projectErrors[id] = '$e';
    } finally {
      if (scope == _providerGeneration) loadingProjects.remove(id);
      changed();
    }
  }

  Future<void> switchProvider(String id) async {
    if (provider == id) return;
    provider = id;
    selected = '';
    _generation++;
    _providerGeneration++;
    _projectsLoaded = false;
    projects = [];
    projectChats.clear();
    expandedProjects.clear();
    loadingProjects.clear();
    threads = {};
    messages = [];
    queue = [];
    receipt = {};
    notice = '';
    loading = false;
    loadingOlder = false;
    error = '';
    notice = '';
    sendMode = 'send';
    laneErrors.clear();
    changed();
    await loadProjects();
  }

  void setDrawer(bool open) {
    drawerOpen = open;
    changed();
    if (!open && selected.isNotEmpty && online) refreshChat();
  }

  void setVisible(bool value) {
    visible = value;
    if (value) {
      refreshDevice();
      if (!drawerOpen && selected.isNotEmpty) refreshChat();
    }
  }

  Future<void> select(String id, {Json? startedOperation}) async {
    _generation++;
    selected = id;
    drawerOpen = false;
    messages = [];
    queue = [];
    receipt = startedOperation ?? {};
    hasOlder = false;
    olderCursor = null;
    loadingOlder = false;
    _pendingPage = null;
    hasNewMessages = false;
    followLatest = true;
    laneErrors.clear();
    error = '';
    loading = true;
    changed();
    await refreshChat();
  }

  Future<void> refreshChat() async {
    if (selected.isEmpty || !online) return;
    final id = selected, generation = _generation, goalEpoch = _goalEpoch;
    bool current() => !disposed && selected == id && generation == _generation;
    final paths = {
      'thread': '',
      'messages': '/messages?limit=40',
      if (supportsQueue) 'queue': '/queue',
    };
    final operation = receipt['operationId'];
    if (operation is String &&
        receipt['result']?['threadId'] == id &&
        ![
          'completed',
          'failed',
          'interrupted',
        ].contains(receipt['delivery']?['turnStatus']) &&
        ![
          'failed',
          'outcome_unknown',
          'cancelled',
        ].contains(receipt['requestStatus'])) {
      paths['receipt'] = '/api/operations/$operation';
    }
    await Future.wait(
      paths.entries.map((entry) async {
        final key = '$generation:${entry.key}';
        if (_pending.contains(key)) return;
        _pending.add(key);
        try {
          final value = await read(
            entry.key == 'receipt'
                ? entry.value
                : '/api/threads/$id${entry.value}',
          );
          if (!current()) return;
          if (entry.key == 'thread') {
            if (value['threadId'] != id) throw const ApiError('返回的对话与当前对话不一致');
            threads[id] = {
              ...value,
              'turns': mergeTurns(chat?['turns'], value['turns']),
              if (goalEpoch != _goalEpoch && chat?['goalState'] != null)
                'goalState': chat!['goalState'],
            };
          } else if (entry.key == 'messages') {
            final incoming = objects(value['messages']);
            if (incoming.any(
              (m) => m['threadId'] != null && m['threadId'] != id,
            )) {
              throw const ApiError('返回的消息不属于当前对话');
            }
            final ids = incoming.map((m) => m['itemId']).toSet();
            final overlap = messages.any((m) => ids.contains(m['itemId']));
            final reset =
                messages.isEmpty ||
                incoming.isEmpty ||
                value['hasMore'] != true ||
                !overlap;
            if (reset && messages.isNotEmpty && !followLatest) {
              _pendingPage = value;
              hasNewMessages = true;
            } else {
              if (reset) {
                messages = incoming;
                olderCursor = value['olderCursor'];
                hasOlder = value['hasMore'] == true;
              } else {
                messages = [
                  ...messages.where(
                    (m) => compareMessage(m, incoming.first) < 0,
                  ),
                  ...incoming,
                ];
              }
            }
            _mergePageTurns(value);
            loading = false;
          } else if (entry.key == 'queue') {
            queue = objects(value['items']);
          } else if (receipt['operationId'] == operation) {
            receipt = value;
          }
          laneErrors.remove(entry.key);
        } catch (e) {
          if (current()) {
            laneErrors[entry.key] = '$e';
            if (entry.key == 'messages') loading = false;
            _authenticationError(e);
          }
        } finally {
          _pending.remove(key);
          if (current()) changed();
        }
      }),
    );
  }

  Future<void> loadOlder() async {
    if (loadingOlder || olderCursor == null || !online) return;
    final generation = _generation, id = selected, cursor = olderCursor!;
    loadingOlder = true;
    followLatest = false;
    changed();
    try {
      final value = await read(
        '/api/threads/$id/messages?limit=40&before=${Uri.encodeQueryComponent(cursor)}',
      );
      if (generation != _generation || disposed) return;
      if (objects(
        value['messages'],
      ).any((m) => m['threadId'] != null && m['threadId'] != id)) {
        throw const ApiError('历史消息不属于当前对话');
      }
      final merged = <String, Json>{
        for (final item in [...objects(value['messages']), ...messages])
          item['itemId'] as String: item,
      };
      messages = merged.values.toList()..sort(compareMessage);
      _mergePageTurns(value);
      olderCursor = value['olderCursor'];
      hasOlder = value['hasMore'] == true;
    } catch (e) {
      if (generation == _generation) _error(e);
    } finally {
      if (generation == _generation) {
        loadingOlder = false;
        changed();
      }
    }
  }

  void jumpLatest() {
    if (_pendingPage != null) {
      messages = objects(_pendingPage!['messages']);
      olderCursor = _pendingPage!['olderCursor'];
      hasOlder = _pendingPage!['hasMore'] == true;
      _pendingPage = null;
    }
    followLatest = true;
    hasNewMessages = false;
    changed();
    refreshChat();
  }

  void _mergePageTurns(Json page) {
    if (page['turns'] is List && chat != null) {
      threads[selected] = {
        ...chat!,
        'turns': mergeTurns(chat!['turns'], page['turns']),
      };
    }
  }

  Future<Json?> mutate(String path, String method, Json body) async {
    if (busy) return null;
    final scope = _generation, chosen = provider;
    busy = true;
    error = '';
    changed();
    final fingerprint = jsonEncode([chosen, path, method, body]);
    final key = _uncertainWrites.putIfAbsent(fingerprint, requestKey);
    try {
      final value = await api.bridge(
        providerPath(chosen, path),
        method: method,
        body: body,
        key: key,
      );
      final startingClaude =
          chosen == 'claude' &&
          value['requestStatus'] == 'in_flight' &&
          value['result']?['executionState'] == 'starting';
      if (!startingClaude &&
          ['in_flight', 'outcome_unknown'].contains(value['requestStatus'])) {
        throw const ApiError('操作结果暂不确定，请刷新核对后再操作', 409, true);
      }
      if (value['requestStatus'] == 'failed') {
        throw ApiError(value['result']?['error']?.toString() ?? '指令未执行', 409);
      }
      _uncertainWrites.remove(fingerprint);
      if (scope == _generation &&
          (chosen != 'claude' ||
              [
                'send',
                'create',
                'queue_add',
                'queue_send',
              ].contains(value['kind']) ||
              '${value['kind']}'.startsWith('goal_'))) {
        receipt = value;
      }
      return value;
    } catch (e) {
      if (e is ApiError && e.status > 0 && e.status < 500 && !e.uncertain) {
        _uncertainWrites.remove(fingerprint);
      }
      if (scope == _generation) _error(e);
      return null;
    } finally {
      busy = false;
      changed();
    }
  }

  Future<bool> send() async {
    final text = draft;
    if (text.trim().isEmpty || !canSendDraft) return false;
    final key = draftKey,
        id = selected,
        generation = _generation,
        mode = effectiveSendMode;
    final value = await mutate(
      '/api/threads/$id/${mode == 'queue' ? 'queue' : 'messages'}',
      'POST',
      {'text': text},
    );
    if (value == null) return false;
    if (drafts[key] == text) drafts[key] = '';
    if (generation == _generation) {
      if (provider != 'claude') {
        notice = mode == 'queue' ? '已加入原生队列' : 'Mac 已受理，正在回读对话';
      } else {
        notice = '';
      }
      followLatest = true;
      unawaited(refreshChat());
    }
    changed();
    return true;
  }

  Future<bool> goalAction(String method, Json changes) async {
    final id = selected, generation = _generation;
    final value = await mutate('/api/threads/$id/goal', method, {
      ...changes,
      'revision': goalState?['revision'],
    });
    if (generation == _generation) {
      final result = value?['result'];
      if (result is Map && result['acknowledged'] == true) {
        _goalEpoch++;
        threads[id] = {
          ...?chat,
          'goalState': {
            ...?goalState,
            'goal': result['goal'],
            'revision': result['revision'],
          },
        };
      }
      unawaited(refreshChat());
      if (value != null) {
        notice = result?['nativeConfirmed'] == true
            ? '$providerName 已确认目标更新'
            : '目标已受理，请核对最新状态';
      }
    }
    if (value == null) return false;
    changed();
    return true;
  }

  Future<void> queueAction(
    String action, {
    Json? item,
    String? text,
    List<String>? ids,
  }) async {
    final generation = _generation;
    final base = '/api/threads/$selected/queue';
    final path = action == 'order'
        ? '$base/order'
        : item == null
        ? base
        : '$base/${item['id']}${action == 'send' ? '/send' : ''}';
    final method = action == 'order'
        ? 'PUT'
        : action == 'send'
        ? 'POST'
        : action == 'edit'
        ? 'PATCH'
        : 'DELETE';
    final value = await mutate(path, method, {'text': ?text, 'ids': ?ids});
    if (value != null && generation == _generation) {
      notice = action == 'send'
          ? value['requestStatus'] == 'queued'
                ? '已移到队首，等待执行'
                : 'Mac 已受理，正在核对同一对话'
          : '队列已更新';
      unawaited(refreshChat());
    }
  }

  Future<void> stopClaude() async {
    final id = selected, run = chat?['executionId'], generation = _generation;
    if (!canStopClaude || run is! String) return;
    final result = await mutate('/api/threads/$id/stop', 'POST', {
      'executionId': run,
    });
    if (result != null && generation == _generation) {
      notice = '已停止当前指令，队列已暂停';
      unawaited(refreshChat());
    }
  }

  Future<bool> saveClaudeSettings(Json options) async {
    final id = selected, generation = _generation;
    if (provider != 'claude' || chat?['claudeSettings']?['writable'] != true) {
      return false;
    }
    final result = await mutate('/api/threads/$id/settings', 'PATCH', options);
    if (result != null && generation == _generation) {
      notice = '设定将从下一条 Claude 指令生效';
      unawaited(refreshChat());
    }
    return result != null;
  }

  Future<JsonList> files() async {
    final chosen = provider, id = selected;
    final value = await api.bridge(
      providerPath(chosen, '/api/threads/$id/artifacts'),
    );
    return objects(
      value['artifacts'],
    ).map((file) => {...file, 'threadId': id, '_provider': chosen}).toList();
  }

  Future<Uri> fileUrl(Json artifact) => api.artifactUrl(
    providerPath(
      artifact['_provider'] as String? ?? provider,
      '/api/threads/${artifact['threadId']}/artifacts/${artifact['id']}/download',
    ),
  );
  Future<bool> createChat(String cwd, String title, String text) async {
    if (!canCreate || (provider == 'claude' && text.trim().isEmpty)) {
      return false;
    }
    final generation = _generation;
    final value = await mutate('/api/threads', 'POST', {
      'cwd': cwd,
      'title': title,
      'text': text,
    });
    final id = value?['result']?['threadId'];
    if (id is! String) return false;
    if (generation != _generation) return true;
    await select(id, startedOperation: provider == 'claude' ? value : null);
    for (final project in expandedProjects.toList()) {
      loadProject(project);
    }
    return true;
  }

  void _authenticationError(Object e) {
    if (e is ApiError && e.status == 401) {
      _clearAccount();
    }
  }

  void _error(Object e) {
    _authenticationError(e);
    error = '$e';
    changed();
  }

  @override
  void dispose() {
    disposed = true;
    _generation++;
    _conversationTimer?.cancel();
    _metadataTimer?.cancel();
    _projectTimer?.cancel();
    api.close();
    super.dispose();
  }
}
