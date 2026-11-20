// The Markdown renderer still uses the SDK Material types.
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import '../core/api.dart';
import '../core/workspace.dart';
import 'claude_settings.dart';
import 'app.dart';

class ComposerDock extends StatefulWidget {
  final Workspace state;
  final VoidCallback files, newChat;
  final bool keyboardVisible;
  const ComposerDock({
    super.key,
    required this.state,
    required this.files,
    required this.newChat,
    this.keyboardVisible = false,
  });
  @override
  State<ComposerDock> createState() => _ComposerDockState();
}

class _ComposerDockState extends State<ComposerDock> {
  final input = TextEditingController(), focus = FocusNode();
  bool menuOpen = false;
  int focusRevision = 0;
  @override
  void initState() {
    super.initState();
    focus.addListener(_focusChanged);
  }

  void _focusChanged() {
    focusRevision++;
    if (mounted) setState(() {});
  }

  @override
  void didUpdateWidget(ComposerDock oldWidget) {
    super.didUpdateWidget(oldWidget);
    // Android's keyboard-dismiss button can hide the IME without blurring the
    // field. Finish that interaction, while allowing a newer focus to win.
    if (oldWidget.keyboardVisible && !widget.keyboardVisible && !menuOpen) {
      final revision = focusRevision;
      WidgetsBinding.instance.addPostFrameCallback((_) {
        if (mounted &&
            !widget.keyboardVisible &&
            !menuOpen &&
            focusRevision == revision) {
          focus.unfocus();
        }
      });
    }
  }

  void _menu(bool open) {
    if (mounted) setState(() => menuOpen = open);
  }

  @override
  void dispose() {
    focus.removeListener(_focusChanged);
    focus.dispose();
    input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final state = widget.state;
    if (input.text != state.draft) {
      input.value = TextEditingValue(
        text: state.draft,
        selection: TextSelection.collapsed(offset: state.draft.length),
      );
    }
    final goal = state.goal;
    final expanded = focus.hasFocus || menuOpen;
    final duration = MediaQuery.disableAnimationsOf(context)
        ? Duration.zero
        : const Duration(milliseconds: 260);
    final bottom = widget.keyboardVisible
        ? 8.0
        : MediaQuery.viewPaddingOf(context).bottom + 10;
    final settings = state.online && !state.laneErrors.containsKey('thread')
        ? (state.chat?['modelSettings'] as Map?)
        : null;
    const models = {
      'gpt-6.1-sol': 'GPT-6.1 Sol',
      'gpt-6-astra': 'GPT-6 Astra',
      'gpt-6.1-terra': 'GPT-6.1 Terra',
    };
    final result = state.receipt['result'] as Map?;
    final delivery = state.receipt['delivery'] as Map?;
    final writeMessage = state.chat?['writeMessage']?.toString();
    final deliveryText = state.receipt['requestStatus'] == 'failed'
        ? ((result?['error'])?.toString() ?? '指令未执行')
        : state.receipt['requestStatus'] == 'outcome_unknown'
        ? '执行结果待核对，请先查看当前对话'
        : state.receipt['requestStatus'] == 'cancelled'
        ? '已移出待执行队列'
        : delivery?['turnStatus'] == 'interrupted'
        ? '本轮已停止'
        : delivery?['turnStatus'] == 'completed'
        ? '这一轮已完成 · 已核对原会话'
        : delivery?['receivedInHistory'] == true
        ? '同一对话已收到 · 等待后续回复'
        : result?['deliveryState'] == 'queued'
        ? state.provider == 'claude'
              ? '已加入 AIRemote 队列 · 等待执行'
              : '已保存到原生队列 · 等待执行'
        : state.provider == 'claude' &&
              state.receipt['requestStatus'] == 'in_flight'
        ? 'Claude Code 正在处理，等待会话回执'
        : state.receipt['operationId'] != null &&
              [
                'send',
                'queue_send',
                'queue_add',
                'create',
              ].contains(state.receipt['kind'])
        ? 'Mac 已受理 · 正在核对原对话'
        : writeMessage;
    return AnimatedPadding(
      duration: duration,
      curve: Curves.easeOutCubic,
      padding: EdgeInsets.fromLTRB(12, 5, 12, bottom),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 900),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (state.notice.isNotEmpty)
              Padding(
                padding: const EdgeInsets.only(bottom: 5),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        state.notice,
                        style: const TextStyle(fontSize: 10, color: muted),
                      ),
                    ),
                    InkWell(
                      onTap: () {
                        state.notice = '';
                        state.changed();
                      },
                      child: const Padding(
                        padding: EdgeInsets.all(6),
                        child: Icon(Icons.close, size: 12, color: muted),
                      ),
                    ),
                  ],
                ),
              ),
            if (deliveryText != null && deliveryText.isNotEmpty)
              Align(
                alignment: Alignment.centerLeft,
                child: Padding(
                  padding: const EdgeInsets.only(left: 7, bottom: 6),
                  child: Text(
                    deliveryText,
                    style: const TextStyle(fontSize: 11, color: muted),
                  ),
                ),
              ),
            if (state.provider == 'codex' ||
                state.provider == 'claude' && settings != null)
              Padding(
                padding: const EdgeInsets.fromLTRB(7, 0, 7, 6),
                child: Row(
                  children: [
                    Expanded(
                      child: Text(
                        settings == null
                            ? '模型设置待同步'
                            : '${models[settings['model']] ?? settings['model'] ?? '模型待同步'}${settings['reasoningEffort'] == null ? '' : ' · ${settings['reasoningEffort']}'}',
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 11, color: muted),
                      ),
                    ),
                    if (settings?['collaborationMode'] != null)
                      Text(
                        settings!['collaborationMode'] == 'plan' ? '计划' : '工作',
                        style: const TextStyle(fontSize: 11, color: muted),
                      ),
                    if (settings?['permissionMode'] == 'dangerFullAccess')
                      const Padding(
                        padding: EdgeInsets.only(left: 9),
                        child: Text(
                          '完全访问',
                          style: TextStyle(
                            fontSize: 10,
                            color: Color(0xffa0763b),
                          ),
                        ),
                      ),
                    if (state.provider == 'claude' &&
                        state.chat?['claudeSettings']?['writable'] == true)
                      IconButton(
                        tooltip: 'Claude 工作设置',
                        visualDensity: VisualDensity.compact,
                        onPressed: state.busy
                            ? null
                            : () => showClaudeSettings(context, state),
                        icon: const Icon(Icons.tune, size: 17),
                      ),
                    if (state.canStopClaude)
                      IconButton(
                        tooltip: '停止当前 Claude 指令',
                        visualDensity: VisualDensity.compact,
                        onPressed: state.stopClaude,
                        icon: const Icon(Icons.stop_circle_outlined, size: 20),
                      ),
                  ],
                ),
              ),
            TweenAnimationBuilder<double>(
              tween: Tween(end: expanded ? 1 : 0),
              duration: duration,
              curve: Curves.easeOutCubic,
              builder: (context, progress, _) => Padding(
                padding: EdgeInsets.symmetric(horizontal: 22 * (1 - progress)),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (state.queue.isNotEmpty) _QueueDock(state: state),
                    if (goal != null)
                      GoalDock(state: state, onMenuChanged: _menu),
                    _surface(context, expanded, progress, duration),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _surface(
    BuildContext context,
    bool expanded,
    double progress,
    Duration duration,
  ) {
    final state = widget.state;
    return TextFieldTapRegion(
      key: const Key('composer-input-region'),
      child: Container(
        key: const Key('message-composer'),
        width: double.infinity,
        decoration: BoxDecoration(
          color: Colors.white,
          border: Border.all(color: const Color(0xffe7e7eb)),
          borderRadius: BorderRadius.circular(34 - 8 * progress),
        ),
        constraints: const BoxConstraints(minHeight: 56),
        child: Stack(
          fit: StackFit.passthrough,
          children: [
            Padding(
              padding: EdgeInsets.lerp(
                const EdgeInsets.fromLTRB(54, 15, 54, 15),
                const EdgeInsets.fromLTRB(16, 12, 16, 60),
                progress,
              )!,
              child: AnimatedSize(
                duration: duration,
                curve: Curves.easeOutCubic,
                alignment: Alignment.bottomCenter,
                child: Semantics(
                  label: '新的 AI 指令',
                  // One EditableText stays mounted throughout the morph. Its
                  // focus, selection, composing range and IME connection survive.
                  child: TextField(
                    key: const Key('composer-input'),
                    controller: input,
                    focusNode: focus,
                    minLines: 1,
                    maxLines: expanded ? (widget.keyboardVisible ? 3 : 5) : 1,
                    textInputAction: TextInputAction.newline,
                    textCapitalization: TextCapitalization.sentences,
                    style: const TextStyle(fontSize: 16, height: 1.6),
                    decoration: InputDecoration(
                      hintText: state.effectiveSendMode == 'queue'
                          ? '写下下一步指令…'
                          : null,
                      border: InputBorder.none,
                      enabledBorder: InputBorder.none,
                      focusedBorder: InputBorder.none,
                      fillColor: Colors.transparent,
                      contentPadding: EdgeInsets.zero,
                      isDense: true,
                    ),
                    onChanged: state.setDraft,
                    onTapOutside: (_) => focus.unfocus(),
                  ),
                ),
              ),
            ),
            Positioned(left: 4, bottom: 4, child: _actions(context)),
            Positioned(right: 4, bottom: 4, child: _send(state)),
            if (state.supportsQueue)
              Positioned(
                right: 53,
                bottom: 4,
                child: ExcludeFocus(
                  excluding: progress < .95,
                  child: IgnorePointer(
                    ignoring: progress < .95,
                    child: ExcludeSemantics(
                      excluding: progress < .95,
                      child: Opacity(
                        opacity: progress,
                        child: PopupMenuButton<String>(
                          tooltip: '指令发送方式',
                          initialValue: state.effectiveSendMode,
                          onOpened: () => _menu(true),
                          onCanceled: () => _menu(false),
                          onSelected: (value) {
                            _menu(false);
                            state.sendMode = value;
                            state.changed();
                            focus.requestFocus();
                          },
                          itemBuilder: (_) => [
                            PopupMenuItem(
                              value: 'send',
                              enabled:
                                  state.provider != 'claude' ||
                                  !state.isRunning,
                              child: const Text('立即发送'),
                            ),
                            PopupMenuItem(
                              value: 'queue',
                              child: Text(
                                state.provider == 'claude'
                                    ? '加入 AIRemote 队列'
                                    : '加入原生队列',
                              ),
                            ),
                          ],
                          child: Padding(
                            padding: const EdgeInsets.symmetric(
                              horizontal: 9,
                              vertical: 12,
                            ),
                            child: Text(
                              state.effectiveSendMode == 'queue'
                                  ? '加入队列'
                                  : state.providerName,
                              style: const TextStyle(
                                fontSize: 12,
                                color: muted,
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }

  Widget _send(Workspace state) => IconButton.filled(
    style: IconButton.styleFrom(
      backgroundColor: const Color(0xff141414),
      foregroundColor: Colors.white,
      disabledBackgroundColor: const Color(0xffe4e4e8),
      fixedSize: const Size(40, 40),
    ),
    tooltip: state.effectiveSendMode == 'queue' ? '加入队列' : '发送指令',
    onPressed: !state.canSendDraft || state.draft.trim().isEmpty
        ? null
        : () async {
            await state.send();
          },
    icon: state.busy
        ? const SizedBox.square(
            dimension: 17,
            child: CircularProgressIndicator(
              strokeWidth: 2,
              color: Colors.white,
            ),
          )
        : Icon(
            state.effectiveSendMode == 'queue'
                ? Icons.playlist_add
                : Icons.arrow_upward,
            size: 23,
          ),
  );
  Widget _actions(BuildContext context) => PopupMenuButton<String>(
    tooltip: '对话操作',
    icon: const Icon(Icons.add, color: ink, size: 26),
    position: PopupMenuPosition.over,
    onOpened: () => _menu(true),
    onCanceled: () => _menu(false),
    onSelected: (value) {
      _menu(false);
      switch (value) {
        case 'send':
        case 'queue':
          widget.state.sendMode = value;
          widget.state.changed();
          focus.requestFocus();
        case 'files':
          focus.unfocus();
          widget.files();
        case 'new':
          focus.unfocus();
          widget.newChat();
      }
    },
    itemBuilder: (_) => [
      if (widget.state.supportsQueue)
        PopupMenuItem(
          value: 'send',
          enabled: widget.state.provider != 'claude' || !widget.state.isRunning,
          child: const ListTile(
            dense: true,
            leading: Icon(Icons.arrow_upward, size: 20),
            title: Text('立即发送'),
          ),
        ),
      if (widget.state.supportsQueue)
        PopupMenuItem(
          value: 'queue',
          child: ListTile(
            dense: true,
            leading: const Icon(Icons.playlist_add, size: 20),
            title: Text(
              widget.state.provider == 'claude' ? '加入 AIRemote 队列' : '加入原生队列',
            ),
          ),
        ),
      if (widget.state.supportsQueue) const PopupMenuDivider(),
      const PopupMenuItem(
        value: 'files',
        child: ListTile(
          dense: true,
          leading: Icon(Icons.folder_outlined, size: 20),
          title: Text('对话文件'),
        ),
      ),
      PopupMenuItem(
        value: 'new',
        enabled: widget.state.canCreate,
        child: const ListTile(
          dense: true,
          leading: Icon(Icons.add_comment_outlined, size: 20),
          title: Text('新建对话'),
        ),
      ),
    ],
  );
}

class GoalDock extends StatelessWidget {
  final Workspace state;
  final ValueChanged<bool>? onMenuChanged;
  const GoalDock({super.key, required this.state, this.onMenuChanged});
  @override
  Widget build(BuildContext context) {
    final goal = state.goal!;
    final enabled =
        state.online && state.goalState?['writable'] == true && !state.busy;
    final editable = enabled && state.goalState?['busy'] != true;
    final status = goal['status'] as String?;
    final label = switch (status) {
      'active' => '进行中',
      'paused' => '已暂停',
      'blocked' => '受阻',
      'budgetLimited' => '预算已用完',
      'usageLimited' => '用量受限',
      'complete' => '已完成',
      _ => '状态待同步',
    };
    final color = status == 'active'
        ? const Color(0xff288d76)
        : ['blocked', 'budgetLimited', 'usageLimited'].contains(status)
        ? const Color(0xffa07842)
        : muted;
    final textScaler = MediaQuery.textScalerOf(context);
    final height =
        (textScaler.scale(13) * 1.3 + textScaler.scale(10) * 1.3 + 14)
            .clamp(48.0, double.infinity)
            .toDouble();
    return SizedBox(
      key: const Key('goal-dock'),
      width: double.infinity,
      height: height,
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          // Extend behind the composer's rounded top so the two surfaces
          // meet without the square lower corners protruding at either side.
          const Positioned(
            left: 10,
            right: 10,
            top: 0,
            bottom: -14,
            child: DecoratedBox(
              decoration: BoxDecoration(
                color: Color(0xfff4f4f6),
                border: Border(
                  top: BorderSide(color: Color(0xffe8e8ec)),
                  left: BorderSide(color: Color(0xffe8e8ec)),
                  right: BorderSide(color: Color(0xffe8e8ec)),
                ),
                borderRadius: BorderRadius.vertical(top: Radius.circular(18)),
              ),
            ),
          ),
          Material(
            type: MaterialType.transparency,
            child: Padding(
              padding: const EdgeInsets.fromLTRB(22, 0, 12, 0),
              child: Row(
                children: [
                  const Icon(
                    Icons.track_changes_outlined,
                    size: 16,
                    color: muted,
                  ),
                  const SizedBox(width: 9),
                  Expanded(
                    child: Tooltip(
                      message: '查看目标详情',
                      child: InkWell(
                        borderRadius: BorderRadius.circular(8),
                        onTap: () => showGoal(context, state),
                        child: Padding(
                          padding: const EdgeInsets.symmetric(vertical: 6),
                          child: Column(
                            mainAxisAlignment: MainAxisAlignment.center,
                            crossAxisAlignment: CrossAxisAlignment.start,
                            children: [
                              Text(
                                '${goal['objective']}',
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(
                                  fontSize: 13,
                                  height: 1.3,
                                  fontWeight: FontWeight.w500,
                                  color: Color(0xff55555f),
                                ),
                              ),
                              const SizedBox(height: 2),
                              Row(
                                children: [
                                  Text(
                                    label,
                                    key: const Key('goal-status'),
                                    style: TextStyle(
                                      fontSize: 10,
                                      height: 1.3,
                                      color: color,
                                    ),
                                  ),
                                  const Text(
                                    '  ·  ',
                                    style: TextStyle(
                                      fontSize: 10,
                                      height: 1.3,
                                      color: muted,
                                    ),
                                  ),
                                  Expanded(
                                    child: Text(
                                      durationLabel(
                                        goal['timeUsedSeconds'] as num?,
                                      ),
                                      key: const Key('goal-duration'),
                                      maxLines: 1,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        fontSize: 10,
                                        height: 1.3,
                                        color: muted,
                                      ),
                                    ),
                                  ),
                                ],
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(width: 3),
                  if (status == 'active' ||
                      status == 'paused' ||
                      state.provider != 'claude' && status != 'complete')
                    IconButton(
                      tooltip: status == 'active' ? '暂停目标' : '继续目标',
                      onPressed: enabled && (status == 'active' || editable)
                          ? () => state.goalAction('PATCH', {
                              'status': status == 'active'
                                  ? 'paused'
                                  : 'active',
                            })
                          : null,
                      visualDensity: VisualDensity.compact,
                      constraints: const BoxConstraints.tightFor(
                        width: 40,
                        height: 44,
                      ),
                      icon: Icon(
                        status == 'active'
                            ? Icons.pause_circle_outline
                            : Icons.play_circle_outline,
                        size: 18,
                        color: enabled ? muted : null,
                      ),
                    ),
                  PopupMenuButton<String>(
                    tooltip: '目标更多操作',
                    icon: const Icon(Icons.more_horiz, size: 18, color: muted),
                    constraints: const BoxConstraints(minWidth: 180),
                    padding: const EdgeInsets.all(8),
                    onOpened: () => onMenuChanged?.call(true),
                    onCanceled: () => onMenuChanged?.call(false),
                    onSelected: (value) {
                      onMenuChanged?.call(false);
                      if (value == 'close') {
                        confirmCloseGoal(context, state);
                      } else {
                        showGoal(context, state, edit: value == 'edit');
                      }
                    },
                    itemBuilder: (_) => [
                      const PopupMenuItem(
                        value: 'details',
                        child: Text('目标详情'),
                      ),
                      PopupMenuItem(
                        value: 'edit',
                        enabled: editable,
                        child: const Text('修改目标'),
                      ),
                      const PopupMenuDivider(),
                      PopupMenuItem(
                        value: 'close',
                        enabled: editable,
                        child: const Text(
                          '关闭目标',
                          style: TextStyle(color: Color(0xffac656a)),
                        ),
                      ),
                    ],
                  ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _QueueDock extends StatelessWidget {
  final Workspace state;
  const _QueueDock({required this.state});
  @override
  Widget build(BuildContext context) => Container(
    margin: const EdgeInsets.symmetric(horizontal: 10),
    decoration: BoxDecoration(
      color: Colors.white,
      border: Border.all(color: const Color(0xffe8e8ec)),
      borderRadius: const BorderRadius.vertical(top: Radius.circular(18)),
    ),
    constraints: const BoxConstraints(maxHeight: 170),
    child: SingleChildScrollView(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          if (state.chat?['queuePaused'] == true)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 5),
              child: Text('队列已暂停，选择一条指令继续。'),
            ),
          for (var index = 0; index < state.queue.length; index++)
            _row(context, state.queue[index], index),
        ],
      ),
    ),
  );
  Widget _row(BuildContext context, Json item, int index) => Padding(
    padding: const EdgeInsets.only(left: 8, right: 2),
    child: Row(
      children: [
        const Icon(Icons.subdirectory_arrow_right, size: 16, color: muted),
        const SizedBox(width: 7),
        Expanded(
          child: Text(
            queueText(item),
            maxLines: 2,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 13),
          ),
        ),
        IconButton(
          tooltip: state.provider == 'claude' && state.isRunning
              ? '移到下一条执行'
              : '立即执行队列指令',
          visualDensity: VisualDensity.compact,
          onPressed: state.canManageQueue
              ? () => state.queueAction('send', item: item)
              : null,
          icon: const Icon(Icons.turn_right, size: 20, color: muted),
        ),
        IconButton(
          tooltip: '删除队列指令',
          visualDensity: VisualDensity.compact,
          onPressed: state.canManageQueue
              ? () => state.queueAction('delete', item: item)
              : null,
          icon: const Icon(Icons.delete_outline, size: 18, color: muted),
        ),
        PopupMenuButton<String>(
          tooltip: '队列更多操作',
          enabled: state.canManageQueue,
          icon: const Icon(Icons.more_horiz, size: 20, color: muted),
          onSelected: (action) async {
            if (action == 'edit') {
              final text = await textEditor(context, '编辑队列指令', queueText(item));
              if (text != null) {
                await state.queueAction('edit', item: item, text: text);
              }
            }
            if (action == 'up' || action == 'down') {
              final ids = state.queue.map((v) => v['id'] as String).toList();
              final to = index + (action == 'up' ? -1 : 1);
              if (to < 0 || to >= ids.length) return;
              final old = ids[index];
              ids[index] = ids[to];
              ids[to] = old;
              await state.queueAction('order', ids: ids);
            }
            if (action == 'clear' && context.mounted) {
              final ok = await showDialog<bool>(
                context: context,
                builder: (c) => AlertDialog(
                  title: const Text('清空当前队列？'),
                  content: const Text('已发送的消息和正在执行的任务会保留。'),
                  actions: [
                    TextButton(
                      onPressed: () => Navigator.pop(c, false),
                      child: const Text('保留'),
                    ),
                    TextButton(
                      onPressed: () => Navigator.pop(c, true),
                      child: const Text('清空'),
                    ),
                  ],
                ),
              );
              if (ok == true) {
                await state.queueAction(
                  'clear',
                  ids: state.queue.map((v) => v['id'] as String).toList(),
                );
              }
            }
          },
          itemBuilder: (_) => [
            const PopupMenuItem(value: 'edit', child: Text('编辑指令')),
            if ((state.descriptor['features'] as List? ?? []).contains(
              'queue-reorder',
            )) ...[
              PopupMenuItem(
                value: 'up',
                enabled: index > 0,
                child: const Text('向上移'),
              ),
              PopupMenuItem(
                value: 'down',
                enabled: index < state.queue.length - 1,
                child: const Text('向下移'),
              ),
            ],
            const PopupMenuItem(value: 'clear', child: Text('清空队列')),
          ],
        ),
      ],
    ),
  );
}

Future<String?> textEditor(
  BuildContext context,
  String title,
  String initial,
) async {
  final controller = TextEditingController(text: initial);
  final value = await showDialog<String>(
    context: context,
    builder: (c) => AlertDialog(
      title: Text(title),
      content: TextField(
        controller: controller,
        autofocus: true,
        minLines: 3,
        maxLines: 8,
        decoration: const InputDecoration(hintText: '输入指令'),
      ),
      actions: [
        TextButton(onPressed: () => Navigator.pop(c), child: const Text('取消')),
        TextButton(
          onPressed: () {
            if (controller.text.trim().isNotEmpty) {
              Navigator.pop(c, controller.text.trim());
            }
          },
          child: const Text('保存'),
        ),
      ],
    ),
  );
  controller.dispose();
  return value;
}

Future<void> confirmCloseGoal(BuildContext context, Workspace state) async {
  final ok = await showDialog<bool>(
    context: context,
    builder: (c) => AlertDialog(
      title: const Text('关闭这个目标？'),
      content: const Text('目标会从当前对话移除，停止自动推进。聊天消息会保留，当前轮次仍可能继续。'),
      actions: [
        TextButton(
          onPressed: () => Navigator.pop(c, false),
          child: const Text('保留目标'),
        ),
        TextButton(
          onPressed: () => Navigator.pop(c, true),
          child: const Text('关闭目标', style: TextStyle(color: Color(0xffb16469))),
        ),
      ],
    ),
  );
  if (ok == true) await state.goalAction('DELETE', {});
}

void showGoal(BuildContext context, Workspace state, {bool edit = false}) {
  showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => GoalSheet(state: state, startEditing: edit),
  );
}

class GoalSheet extends StatefulWidget {
  final Workspace state;
  final bool startEditing;
  const GoalSheet({super.key, required this.state, this.startEditing = false});
  @override
  State<GoalSheet> createState() => _GoalSheetState();
}

class _GoalSheetState extends State<GoalSheet> {
  final objective = TextEditingController(), budget = TextEditingController();
  bool editing = false, paused = false, submitting = false;
  String? revision;
  @override
  void initState() {
    super.initState();
    reset();
    editing = widget.startEditing || widget.state.goal == null;
  }

  void reset() {
    objective.text = widget.state.goal?['objective'] ?? '';
    budget.text = widget.state.goal?['tokenBudget']?.toString() ?? '';
    revision = widget.state.goalState?['revision'];
  }

  @override
  void dispose() {
    objective.dispose();
    budget.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.state,
    builder: (_, _) {
      final state = widget.state, goal = state.goal;
      final claude = state.provider == 'claude';
      final enabled =
          state.online &&
          state.goalState?['writable'] == true &&
          !state.busy &&
          !submitting;
      final editable = enabled && state.goalState?['busy'] != true;
      final stale = editing && revision != state.goalState?['revision'];
      return SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
            22,
            0,
            22,
            MediaQuery.viewInsetsOf(context).bottom + 22,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              Row(
                children: [
                  Text(
                    goal == null ? '设置对话目标' : '对话目标',
                    style: const TextStyle(
                      fontSize: 21,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const Spacer(),
                  IconButton(
                    tooltip: '关闭目标详情',
                    onPressed: submitting ? null : () => Navigator.pop(context),
                    icon: const Icon(Icons.close, size: 20),
                  ),
                ],
              ),
              if (state.goalState?['writable'] != true)
                Padding(
                  padding: const EdgeInsets.symmetric(vertical: 14),
                  child: Text(
                    state.goalState?['message'] ?? '目标状态暂未同步',
                    style: const TextStyle(color: muted, fontSize: 13),
                  ),
                ),
              if (state.error.isNotEmpty)
                Text(
                  state.error,
                  style: const TextStyle(color: Colors.red, fontSize: 12),
                ),
              if (editing || goal == null) ...[
                const SizedBox(height: 12),
                TextField(
                  controller: objective,
                  minLines: 3,
                  maxLines: 8,
                  maxLength: claude ? 4000 : 32000,
                  decoration: InputDecoration(
                    labelText: '目标',
                    hintText: '告诉 ${state.providerName} 需要持续完成什么…',
                  ),
                ),
                if (!claude) ...[
                  const SizedBox(height: 14),
                  TextField(
                    controller: budget,
                    keyboardType: TextInputType.number,
                    decoration: const InputDecoration(
                      labelText: 'Token 预算（可选）',
                      hintText: '留空使用默认预算',
                    ),
                  ),
                ],
                if (goal == null && !claude)
                  CheckboxListTile(
                    contentPadding: EdgeInsets.zero,
                    value: paused,
                    onChanged: (v) => setState(() {
                      paused = v ?? false;
                    }),
                    title: const Text(
                      '先保存为暂停状态',
                      style: TextStyle(fontSize: 13),
                    ),
                    controlAffinity: ListTileControlAffinity.leading,
                  ),
                if (stale)
                  const Text(
                    '目标已经变化，请返回查看最新状态后再修改。',
                    style: TextStyle(color: Colors.red, fontSize: 12),
                  ),
                const SizedBox(height: 15),
                Align(
                  alignment: Alignment.centerRight,
                  child: FilledButton(
                    onPressed: !editable || stale
                        ? null
                        : () async {
                            final tokens = budget.text.trim().isEmpty
                                ? null
                                : int.tryParse(budget.text);
                            if (objective.text.trim().isEmpty ||
                                (budget.text.trim().isNotEmpty &&
                                    (tokens == null || tokens <= 0))) {
                              return;
                            }
                            setState(() {
                              submitting = true;
                            });
                            final ok = await state
                                .goalAction(goal == null ? 'POST' : 'PATCH', {
                                  'objective': objective.text.trim(),
                                  if (!claude) 'tokenBudget': tokens,
                                  if (goal == null)
                                    'status': paused ? 'paused' : 'active',
                                });
                            if (mounted) {
                              setState(() {
                                submitting = false;
                              });
                              if (ok && context.mounted) Navigator.pop(context);
                            }
                          },
                    child: Text(
                      goal == null
                          ? paused
                                ? '保存目标'
                                : '开始目标'
                          : '保存修改',
                    ),
                  ),
                ),
              ] else ...[
                const SizedBox(height: 12),
                Text(
                  goalLabel(goal['status']),
                  style: const TextStyle(color: muted, fontSize: 12),
                ),
                const SizedBox(height: 10),
                Text(
                  '${goal['objective']}',
                  style: const TextStyle(fontSize: 16, height: 1.8),
                ),
                const SizedBox(height: 18),
                Text(
                  '已用 ${goal['tokensUsed'] ?? 0} tokens${goal['tokenBudget'] == null ? '' : ' / ${goal['tokenBudget']}'} · 已处理 ${durationLabel(goal['timeUsedSeconds'] as num?)}',
                  style: const TextStyle(color: muted, fontSize: 12),
                ),
                const SizedBox(height: 18),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    OutlinedButton.icon(
                      onPressed: editable
                          ? () => setState(() {
                              reset();
                              editing = true;
                            })
                          : null,
                      icon: const Icon(Icons.edit_outlined, size: 16),
                      label: const Text('修改目标'),
                    ),
                    if (!claude ||
                        ['active', 'paused'].contains(goal['status']))
                      OutlinedButton.icon(
                        onPressed:
                            enabled && (goal['status'] == 'active' || editable)
                            ? () => state.goalAction('PATCH', {
                                'status': goal['status'] == 'active'
                                    ? 'paused'
                                    : 'active',
                              })
                            : null,
                        icon: Icon(
                          goal['status'] == 'active'
                              ? Icons.pause
                              : Icons.play_arrow,
                          size: 17,
                        ),
                        label: Text(
                          goal['status'] == 'active' ? '暂停目标' : '继续目标',
                        ),
                      ),
                    OutlinedButton(
                      onPressed: editable
                          ? () async {
                              await confirmCloseGoal(context, state);
                              if (context.mounted && state.goal == null) {
                                Navigator.pop(context);
                              }
                            }
                          : null,
                      child: const Text('关闭目标'),
                    ),
                  ],
                ),
                const SizedBox(height: 15),
                Text(
                  claude
                      ? '暂停会停止这轮执行，并保留目标。暂停后可以修改或关闭目标。'
                      : '暂停后不再自动推进目标，正在执行的这一轮仍可能继续。',
                  style: const TextStyle(
                    fontSize: 11,
                    color: muted,
                    height: 1.7,
                  ),
                ),
              ],
            ],
          ),
        ),
      );
    },
  );
}

void showNewChat(BuildContext context, Workspace state) {
  showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (_) => NewChatSheet(state: state),
  );
}

class NewChatSheet extends StatefulWidget {
  final Workspace state;
  const NewChatSheet({super.key, required this.state});
  @override
  State<NewChatSheet> createState() => _NewChatSheetState();
}

class _NewChatSheetState extends State<NewChatSheet> {
  final title = TextEditingController(), message = TextEditingController();
  String? cwd;
  bool submitting = false;
  @override
  void initState() {
    super.initState();
    cwd = widget.state.createPaths.firstOrNull;
  }

  @override
  void dispose() {
    title.dispose();
    message.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.state,
    builder: (_, _) {
      final paths = widget.state.createPaths;
      final requiresMessage = widget.state.provider == 'claude';
      return SafeArea(
        top: false,
        child: SingleChildScrollView(
          padding: EdgeInsets.fromLTRB(
            22,
            0,
            22,
            MediaQuery.viewInsetsOf(context).bottom + 22,
          ),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: [
              const Text(
                '新建对话',
                style: TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
              ),
              const SizedBox(height: 18),
              DropdownButtonFormField<String>(
                initialValue: cwd,
                isExpanded: true,
                items: paths
                    .map(
                      (p) => DropdownMenuItem(
                        value: p,
                        child: Text(
                          p.split('/').last,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    )
                    .toList(),
                onChanged: (v) => cwd = v,
                decoration: const InputDecoration(labelText: '项目'),
              ),
              const SizedBox(height: 14),
              TextField(
                controller: title,
                decoration: const InputDecoration(
                  labelText: '对话名称',
                  hintText: '新的思路',
                ),
              ),
              const SizedBox(height: 14),
              TextField(
                controller: message,
                minLines: 3,
                maxLines: 7,
                onChanged: requiresMessage ? (_) => setState(() {}) : null,
                decoration: InputDecoration(
                  labelText: requiresMessage ? '第一条指令' : '第一条指令（可选）',
                ),
              ),
              if (widget.state.error.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.only(top: 10),
                  child: Text(
                    widget.state.error,
                    style: const TextStyle(color: Colors.red, fontSize: 12),
                  ),
                ),
              const SizedBox(height: 20),
              Align(
                alignment: Alignment.centerRight,
                child: FilledButton(
                  onPressed:
                      submitting ||
                          !paths.contains(cwd) ||
                          requiresMessage && message.text.trim().isEmpty
                      ? null
                      : () async {
                          setState(() {
                            submitting = true;
                          });
                          final ok = await widget.state.createChat(
                            cwd!,
                            title.text.trim().isEmpty
                                ? '新对话'
                                : title.text.trim(),
                            message.text.trim(),
                          );
                          if (mounted) {
                            setState(() {
                              submitting = false;
                            });
                            if (ok && context.mounted) Navigator.pop(context);
                          }
                        },
                  child: const Text('创建对话'),
                ),
              ),
            ],
          ),
        ),
      );
    },
  );
}
