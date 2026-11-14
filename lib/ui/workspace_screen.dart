import 'dart:math';
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollDirection;
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:url_launcher/url_launcher.dart';
import '../core/api.dart';
import '../core/platform.dart';
import '../core/workspace.dart';
import 'app.dart';
import 'composer.dart';

class WorkspaceScreen extends StatefulWidget {
  final Workspace state;
  final NativePlatform native;
  final VoidCallback settings;
  const WorkspaceScreen({
    super.key,
    required this.state,
    required this.native,
    required this.settings,
  });
  @override
  State<WorkspaceScreen> createState() => _WorkspaceScreenState();
}

class _WorkspaceScreenState extends State<WorkspaceScreen>
    with WidgetsBindingObserver {
  final scaffold = GlobalKey<ScaffoldState>(), scroll = ScrollController();
  Workspace get state => widget.state;
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState value) =>
      state.setVisible(value == AppLifecycleState.resumed);
  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    scroll.dispose();
    super.dispose();
  }

  void openDrawer() {
    FocusManager.instance.primaryFocus?.unfocus();
    scaffold.currentState?.openDrawer();
  }

  @override
  Widget build(BuildContext context) {
    final padding = MediaQuery.viewPaddingOf(context);
    final keyboardVisible = MediaQuery.viewInsetsOf(context).bottom > 0;
    final selected = state.selected.isNotEmpty;
    return Scaffold(
      key: scaffold,
      backgroundColor: canvas,
      resizeToAvoidBottomInset: true,
      drawerScrimColor: Colors.black.withValues(alpha: .48),
      drawerEdgeDragWidth: 28,
      onDrawerChanged: state.setDrawer,
      drawer: ProjectDrawer(
        state: state,
        newChat: () {
          Navigator.pop(context);
          showNewChat(context, state);
        },
      ),
      body: ColoredBox(
        key: const Key('app-surface'),
        color: canvas,
        child: Column(
          children: [
            Padding(
              padding: EdgeInsets.only(top: padding.top),
              child: SizedBox(
                height: 68,
                child: Row(
                  children: [
                    const SizedBox(width: 8),
                    IconButton(
                      tooltip: '打开项目与对话',
                      onPressed: openDrawer,
                      icon: const Icon(Icons.menu_rounded),
                    ),
                    Expanded(
                      child: Padding(
                        padding: const EdgeInsets.only(left: 5),
                        child: selected
                            ? Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                crossAxisAlignment: CrossAxisAlignment.start,
                                children: [
                                  Row(
                                    children: [
                                      Image.asset(
                                        'assets/${state.provider}.png',
                                        width: 13,
                                        height: 13,
                                      ),
                                      const SizedBox(width: 5),
                                      Text(
                                        state.providerName,
                                        style: const TextStyle(
                                          fontSize: 10,
                                          color: muted,
                                        ),
                                      ),
                                      const SizedBox(width: 5),
                                      Expanded(
                                        child: Text(
                                          '/ ${state.chat?['cwd']?.toString().split('/').last ?? 'Mac'}',
                                          overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(
                                            fontSize: 10,
                                            color: muted,
                                          ),
                                        ),
                                      ),
                                    ],
                                  ),
                                  Text(
                                    state.chat?['title']?.toString() ??
                                        '读取对话中…',
                                    maxLines: 1,
                                    overflow: TextOverflow.ellipsis,
                                    style: const TextStyle(
                                      fontSize: 15,
                                      fontWeight: FontWeight.w600,
                                    ),
                                  ),
                                ],
                              )
                            : const Text(
                                'AIRemote',
                                style: TextStyle(
                                  fontSize: 22,
                                  fontWeight: FontWeight.w700,
                                  letterSpacing: -.5,
                                ),
                              ),
                      ),
                    ),
                    if (selected)
                      IconButton(
                        tooltip: '对话文件',
                        onPressed: () =>
                            showFiles(context, state, widget.native),
                        icon: const Icon(Icons.folder_outlined, size: 21),
                      ),
                    if (selected &&
                        state.provider == 'codex' &&
                        state.goal == null)
                      IconButton(
                        tooltip: '对话目标',
                        onPressed: () => showGoal(context, state),
                        icon: const Icon(
                          Icons.track_changes_outlined,
                          size: 21,
                        ),
                      ),
                    IconButton(
                      tooltip: '应用设置与更新',
                      onPressed: widget.settings,
                      icon: const Icon(Icons.more_horiz, size: 22),
                    ),
                    const SizedBox(width: 3),
                  ],
                ),
              ),
            ),
            if (selected)
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 18, 8),
                child: Row(
                  children: [
                    if (state.online && runtimeType(state.chat) == 'active')
                      const SizedBox.square(
                        dimension: 10,
                        child: CircularProgressIndicator(strokeWidth: 1.5),
                      ),
                    if (state.online && runtimeType(state.chat) == 'active')
                      const SizedBox(width: 6),
                    Text(
                      state.online
                          ? runtimeLabel(state.chat)
                          : state.device['online'] == true
                          ? '连接器离线'
                          : 'Mac 离线',
                      style: TextStyle(
                        fontSize: 11,
                        color:
                            state.online && runtimeType(state.chat) == 'active'
                            ? const Color(0xff239d7b)
                            : muted,
                      ),
                    ),
                    const Spacer(),
                    IconButton(
                      tooltip: '刷新对话',
                      visualDensity: VisualDensity.compact,
                      constraints: const BoxConstraints.tightFor(
                        width: 28,
                        height: 26,
                      ),
                      onPressed: state.online ? state.refreshChat : null,
                      icon: const Icon(Icons.refresh, size: 16, color: muted),
                    ),
                  ],
                ),
              ),
            if (state.error.isNotEmpty)
              _ErrorBar(state.error, () {
                state.error = '';
                state.changed();
              }),
            if (selected && state.laneErrors.isNotEmpty)
              _ErrorBar(
                '同步暂未完成：${state.laneErrors.values.first}',
                state.refreshChat,
              ),
            Expanded(
              child: LayoutBuilder(
                builder: (context, viewport) => Column(
                  children: [
                    Expanded(
                      child: selected
                          ? _messages()
                          : Center(
                              child: Padding(
                                padding: const EdgeInsets.all(32),
                                child: Column(
                                  mainAxisSize: MainAxisSize.min,
                                  children: [
                                    const Text(
                                      '从这里，继续创造。',
                                      textAlign: TextAlign.center,
                                      style: TextStyle(
                                        fontSize: 29,
                                        fontWeight: FontWeight.w500,
                                        letterSpacing: -.8,
                                      ),
                                    ),
                                    const SizedBox(height: 16),
                                    const Text(
                                      '打开侧栏，选择项目中的对话。',
                                      textAlign: TextAlign.center,
                                      style: TextStyle(
                                        color: muted,
                                        fontSize: 14,
                                      ),
                                    ),
                                    const SizedBox(height: 26),
                                    FilledButton(
                                      style: FilledButton.styleFrom(
                                        backgroundColor: ink,
                                        padding: const EdgeInsets.symmetric(
                                          horizontal: 25,
                                          vertical: 14,
                                        ),
                                      ),
                                      onPressed: openDrawer,
                                      child: const Text('选择一个对话'),
                                    ),
                                    TextButton(
                                      onPressed: state.canWrite
                                          ? () => showNewChat(context, state)
                                          : null,
                                      child: const Text('新建对话'),
                                    ),
                                  ],
                                ),
                              ),
                            ),
                    ),
                    if (selected && !state.followLatest)
                      Align(
                        alignment: Alignment.center,
                        child: TextButton.icon(
                          onPressed: () {
                            state.jumpLatest();
                            if (scroll.hasClients) {
                              scroll.animateTo(
                                0,
                                duration: const Duration(milliseconds: 200),
                                curve: Curves.easeOut,
                              );
                            }
                          },
                          icon: const Icon(Icons.arrow_downward, size: 15),
                          label: Text(
                            state.hasNewMessages ? '有新消息 · 回到最新' : '回到最新',
                          ),
                        ),
                      ),
                    if (selected)
                      ConstrainedBox(
                        constraints: BoxConstraints(
                          maxHeight: viewport.maxHeight * .8,
                        ),
                        child: SingleChildScrollView(
                          reverse: true,
                          child: ComposerDock(
                            state: state,
                            keyboardVisible: keyboardVisible,
                            files: () =>
                                showFiles(context, state, widget.native),
                            newChat: () => showNewChat(context, state),
                          ),
                        ),
                      ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _messages() {
    if (state.loading && state.messages.isEmpty) {
      return const Center(child: CircularProgressIndicator(strokeWidth: 2));
    }
    if (state.messages.isEmpty) {
      return const Center(
        child: Text('新的思路，从一句话开始', style: TextStyle(color: muted)),
      );
    }
    final messages = state.messages.reversed.toList();
    return NotificationListener<ScrollNotification>(
      onNotification: (event) {
        final follow = event.metrics.pixels < 80;
        if (state.followLatest != follow) {
          state.followLatest = follow;
          // A scroll notification arrives during layout; update controls next frame.
          WidgetsBinding.instance.addPostFrameCallback((_) => state.changed());
        }
        if (event is UserScrollNotification &&
            event.direction != ScrollDirection.idle &&
            event.metrics.pixels > event.metrics.maxScrollExtent - 100 &&
            state.hasOlder) {
          state.loadOlder();
        }
        return false;
      },
      child: ListView.builder(
        key: ValueKey(state.draftKey),
        controller: scroll,
        reverse: true,
        padding: const EdgeInsets.fromLTRB(18, 10, 18, 20),
        itemCount:
            messages.length +
            (state.hasOlder || state.loadingOlder ? 1 : 0) +
            (runtimeType(state.chat) == 'active' ? 1 : 0),
        itemBuilder: (context, index) {
          if (runtimeType(state.chat) == 'active') {
            if (index == 0) {
              return const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Row(
                  children: [
                    SizedBox.square(
                      dimension: 12,
                      child: CircularProgressIndicator(strokeWidth: 1.6),
                    ),
                    SizedBox(width: 10),
                    Text(
                      '正在执行，回复会自动出现',
                      style: TextStyle(fontSize: 12, color: muted),
                    ),
                  ],
                ),
              );
            }
            index--;
          }
          if (index >= messages.length) {
            return TextButton(
              onPressed: state.loadingOlder ? null : state.loadOlder,
              child: Text(state.loadingOlder ? '正在读取更早消息…' : '向上查看更早消息'),
            );
          }
          return ChatMessage(
            key: ValueKey(messages[index]['itemId']),
            message: messages[index],
            state: state,
            link: (path) =>
                openMessageLink(context, state, widget.native, path),
          );
        },
      ),
    );
  }
}

class _ErrorBar extends StatelessWidget {
  final String text;
  final VoidCallback action;
  const _ErrorBar(this.text, this.action);
  @override
  Widget build(BuildContext context) => Material(
    color: const Color(0xfffff2f3),
    child: Padding(
      padding: const EdgeInsets.only(left: 16),
      child: Row(
        children: [
          Expanded(
            child: Text(
              text,
              maxLines: 3,
              style: const TextStyle(fontSize: 12, color: Color(0xffa85f70)),
            ),
          ),
          IconButton(
            onPressed: action,
            icon: const Icon(Icons.close, size: 17),
          ),
        ],
      ),
    ),
  );
}

class ProjectDrawer extends StatefulWidget {
  final Workspace state;
  final VoidCallback newChat;
  const ProjectDrawer({super.key, required this.state, required this.newChat});
  @override
  State<ProjectDrawer> createState() => _ProjectDrawerState();
}

class _ProjectDrawerState extends State<ProjectDrawer> {
  bool searching = false;
  @override
  Widget build(BuildContext context) {
    final state = widget.state, insets = MediaQuery.viewPaddingOf(context);
    return Drawer(
      key: const Key('sidebar-surface'),
      shape: const RoundedRectangleBorder(),
      width: min(MediaQuery.sizeOf(context).width * .82, 360),
      elevation: 0,
      surfaceTintColor: Colors.transparent,
      backgroundColor: Colors.white,
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Padding(
            padding: EdgeInsets.fromLTRB(22, insets.top + 18, 9, 6),
            child: Row(
              children: [
                const Expanded(
                  child: Align(
                    alignment: Alignment.centerLeft,
                    child: FittedBox(
                      fit: BoxFit.scaleDown,
                      alignment: Alignment.centerLeft,
                      child: Brand(size: 30),
                    ),
                  ),
                ),
                IconButton(
                  tooltip: '搜索项目',
                  onPressed: () => setState(() {
                    searching = !searching;
                    if (!searching) state.search = '';
                  }),
                  icon: const Icon(Icons.search, size: 23),
                ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(20, 8, 15, 12),
            child: Row(
              children: [
                for (final provider in ['codex', 'deepseek'])
                  Padding(
                    padding: const EdgeInsets.only(right: 9),
                    child: Semantics(
                      label:
                          '切换到 ${provider == 'codex' ? 'Codex' : 'DeepSeek'}',
                      selected: state.provider == provider,
                      child: InkWell(
                        borderRadius: BorderRadius.circular(13),
                        onTap: () async {
                          if (state.provider == provider) {
                            final item = state.descriptor;
                            showDialog<void>(
                              context: context,
                              builder: (c) => AlertDialog(
                                title: Text(state.providerName),
                                content: Text(
                                  '${state.device['online'] == true ? item['statusLabel'] ?? '检查中' : 'Mac 离线'}${item['version'] == null ? '' : '\n${item['version']}'}',
                                ),
                                actions: [
                                  TextButton(
                                    onPressed: () => Navigator.pop(c),
                                    child: const Text('知道了'),
                                  ),
                                ],
                              ),
                            );
                          } else {
                            await state.switchProvider(provider);
                          }
                        },
                        child: Container(
                          width: 43,
                          height: 43,
                          decoration: BoxDecoration(
                            color: state.provider == provider
                                ? const Color(0xffeeecff)
                                : const Color(0xfff6f6f8),
                            borderRadius: BorderRadius.circular(13),
                          ),
                          child: Stack(
                            alignment: Alignment.center,
                            children: [
                              Image.asset(
                                'assets/$provider.png',
                                width: 26,
                                height: 26,
                              ),
                              Positioned(
                                right: 4,
                                bottom: 4,
                                child: Container(
                                  width: 7,
                                  height: 7,
                                  decoration: BoxDecoration(
                                    shape: BoxShape.circle,
                                    color:
                                        state.device['online'] == true &&
                                            state.providersById[provider]?['connected'] ==
                                                true
                                        ? const Color(0xff22af89)
                                        : const Color(0xffbbbcc6),
                                    border: Border.all(
                                      color: Colors.white,
                                      width: 1.5,
                                    ),
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(22, 0, 10, 6),
            child: Row(
              children: [
                const Text(
                  '项目',
                  style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
                ),
                const Spacer(),
                IconButton(
                  tooltip: '新建对话',
                  onPressed: state.canWrite ? widget.newChat : null,
                  icon: const Icon(Icons.add, size: 24),
                ),
              ],
            ),
          ),
          if (searching)
            Padding(
              padding: const EdgeInsets.fromLTRB(17, 0, 17, 12),
              child: TextField(
                autofocus: true,
                decoration: const InputDecoration(
                  hintText: '搜索项目',
                  isDense: true,
                  prefixIcon: Icon(Icons.search, size: 18),
                ),
                onChanged: (v) => setState(() {
                  state.search = v;
                }),
              ),
            ),
          Expanded(
            child: ListView(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 3),
              children: [
                for (final project in state.projects.where(
                  (p) => '${p['name']}'.toLowerCase().contains(
                    state.search.toLowerCase(),
                  ),
                )) ...[
                  ListTile(
                    contentPadding: const EdgeInsets.only(left: 10, right: 5),
                    visualDensity: const VisualDensity(vertical: -2),
                    leading: Icon(
                      state.expandedProjects.contains(project['id'])
                          ? Icons.folder_open_outlined
                          : Icons.folder_outlined,
                      size: 23,
                      color: const Color(0xff56565f),
                    ),
                    horizontalTitleGap: 10,
                    title: Text(
                      '${project['name']}',
                      style: const TextStyle(fontSize: 16),
                    ),
                    trailing: Icon(
                      state.expandedProjects.contains(project['id'])
                          ? Icons.keyboard_arrow_down
                          : Icons.chevron_right,
                      size: 16,
                      color: muted,
                    ),
                    onTap: () => state.toggleProject(project['id']),
                  ),
                  if (state.expandedProjects.contains(project['id'])) ...[
                    if (state.loadingProjects.contains(project['id']) &&
                        state.projectChats[project['id']] == null)
                      const Padding(
                        padding: EdgeInsets.fromLTRB(42, 8, 15, 10),
                        child: LinearProgressIndicator(minHeight: 2),
                      ),
                    if (state.projectErrors[project['id']] != null)
                      Padding(
                        padding: const EdgeInsets.only(left: 30),
                        child: TextButton(
                          onPressed: () => state.loadProject(project['id']),
                          child: const Text('读取未完成，点此重试'),
                        ),
                      ),
                    for (final chat
                        in state.projectChats[project['id']] ?? <Json>[])
                      Padding(
                        padding: const EdgeInsets.only(left: 25),
                        child: Material(
                          color: chat['threadId'] == state.selected
                              ? const Color(0xffefeff1)
                              : Colors.transparent,
                          borderRadius: BorderRadius.circular(14),
                          child: InkWell(
                            borderRadius: BorderRadius.circular(14),
                            onTap: () {
                              Navigator.pop(context);
                              state.select(chat['threadId']);
                            },
                            child: Padding(
                              padding: const EdgeInsets.fromLTRB(
                                14,
                                11,
                                13,
                                11,
                              ),
                              child: Row(
                                children: [
                                  Expanded(
                                    child: Text(
                                      '${chat['title']}',
                                      maxLines: 2,
                                      overflow: TextOverflow.ellipsis,
                                      style: const TextStyle(
                                        fontSize: 14,
                                        height: 1.5,
                                      ),
                                    ),
                                  ),
                                  if (state.online &&
                                      runtimeType(chat) == 'active')
                                    const Padding(
                                      padding: EdgeInsets.only(left: 9),
                                      child: SizedBox.square(
                                        dimension: 13,
                                        child: CircularProgressIndicator(
                                          strokeWidth: 1.7,
                                        ),
                                      ),
                                    ),
                                ],
                              ),
                            ),
                          ),
                        ),
                      ),
                  ],
                ],
              ],
            ),
          ),
          Padding(
            padding: EdgeInsets.fromLTRB(18, 7, 18, insets.bottom + 7),
            child: SizedBox(
              width: double.infinity,
              child: TextButton.icon(
                style: TextButton.styleFrom(
                  foregroundColor: muted,
                  alignment: Alignment.centerLeft,
                ),
                onPressed: state.logout,
                icon: const Icon(Icons.logout, size: 19),
                label: const Text('退出登录', style: TextStyle(fontSize: 13)),
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class ChatMessage extends StatelessWidget {
  final Json message;
  final Workspace state;
  final void Function(String) link;
  const ChatMessage({
    super.key,
    required this.message,
    required this.state,
    required this.link,
  });
  @override
  Widget build(BuildContext context) {
    final user = message['role'] == 'user',
        progress = message['phase'] == 'commentary';
    final text = user
        ? userText('${message['text'] ?? ''}')
        : '${message['text'] ?? ''}';
    final turns = objects(state.chat?['turns']);
    final turn = turns
        .where((t) => t['turn_id'] == message['turnId'])
        .firstOrNull;
    final firstReply = state.messages
        .where(
          (m) => m['role'] == 'assistant' && m['turnId'] == message['turnId'],
        )
        .firstOrNull;
    final duration =
        firstReply?['itemId'] == message['itemId'] &&
            turn?['completed_at'] != null &&
            turn?['started_at'] != null
        ? (turn!['completed_at'] as num) - (turn['started_at'] as num)
        : null;
    return Padding(
      padding: EdgeInsets.only(top: user ? 16 : 8, bottom: user ? 14 : 8),
      child: user
          ? Align(
              alignment: Alignment.centerRight,
              child: Container(
                constraints: BoxConstraints(
                  maxWidth: MediaQuery.sizeOf(context).width * .86,
                ),
                padding: const EdgeInsets.symmetric(
                  horizontal: 17,
                  vertical: 12,
                ),
                decoration: BoxDecoration(
                  color: const Color(0xff121212),
                  borderRadius: BorderRadius.circular(23),
                ),
                child: SelectableText(
                  text,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 16,
                    height: 1.7,
                  ),
                ),
              ),
            )
          : Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                if (duration != null)
                  Padding(
                    padding: const EdgeInsets.only(bottom: 9),
                    child: Text(
                      '已处理 ${durationLabel(duration)}',
                      style: const TextStyle(fontSize: 11, color: muted),
                    ),
                  ),
                if (progress)
                  const Padding(
                    padding: EdgeInsets.only(bottom: 7),
                    child: Text(
                      '执行进度',
                      style: TextStyle(fontSize: 10, color: muted),
                    ),
                  ),
                MarkdownBody(
                  data: text,
                  selectable: true,
                  styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context))
                      .copyWith(
                        p: TextStyle(
                          fontSize: progress ? 14 : 16,
                          height: 1.75,
                          color: progress
                              ? const Color(0xff777781)
                              : const Color(0xff33333a),
                        ),
                        code: const TextStyle(
                          fontSize: 13,
                          fontFamily: 'monospace',
                        ),
                        codeblockDecoration: BoxDecoration(
                          color: const Color(0xfff0f0f3),
                          borderRadius: BorderRadius.circular(12),
                        ),
                      ),
                  onTapLink: (_, href, _) {
                    if (href != null) link(href);
                  },
                  imageBuilder: (uri, title, alt) => Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      const Icon(Icons.image_outlined, size: 16, color: muted),
                      Flexible(
                        child: Text(
                          alt ?? title ?? '图片附件',
                          style: const TextStyle(color: muted, fontSize: 13),
                        ),
                      ),
                    ],
                  ),
                ),
                if (!progress)
                  Align(
                    alignment: Alignment.centerLeft,
                    child: IconButton(
                      tooltip: '复制回复',
                      visualDensity: VisualDensity.compact,
                      onPressed: () {
                        Clipboard.setData(ClipboardData(text: text));
                        ScaffoldMessenger.of(
                          context,
                        ).showSnackBar(const SnackBar(content: Text('回复已复制')));
                      },
                      icon: const Icon(
                        Icons.copy_outlined,
                        size: 15,
                        color: muted,
                      ),
                    ),
                  ),
              ],
            ),
    );
  }
}

Future<void> openMessageLink(
  BuildContext context,
  Workspace state,
  NativePlatform native,
  String link,
) async {
  final uri = Uri.tryParse(link);
  final box = context.findRenderObject() as RenderBox?;
  final origin = box == null || !box.hasSize
      ? null
      : box.localToGlobal(Offset.zero) & box.size;
  if (uri?.scheme == 'https') {
    await launchUrl(uri!, mode: LaunchMode.externalApplication);
    return;
  }
  final path = uri?.scheme == 'file' ? uri!.toFilePath() : link;
  if (path.startsWith('/')) {
    final files = await state.files();
    final file = files.where((v) => v['path'] == path).firstOrNull;
    if (file != null) {
      await native.downloadFile(
        state.api,
        file,
        await state.fileUrl(file),
        shareOrigin: origin,
      );
      return;
    }
  }
  if (context.mounted) showFiles(context, state, native);
}

void showFiles(BuildContext context, Workspace state, NativePlatform native) {
  final future = state.files();
  showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (c) => SafeArea(
      top: false,
      child: SizedBox(
        height: MediaQuery.sizeOf(c).height * .65,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Padding(
              padding: EdgeInsets.fromLTRB(22, 0, 22, 15),
              child: Text(
                '对话文件',
                style: TextStyle(fontSize: 21, fontWeight: FontWeight.w600),
              ),
            ),
            Expanded(
              child: FutureBuilder<JsonList>(
                future: future,
                builder: (_, value) {
                  if (value.hasError) {
                    return Center(
                      child: Padding(
                        padding: const EdgeInsets.all(24),
                        child: Text('${value.error}'),
                      ),
                    );
                  }
                  if (!value.hasData) {
                    return const Center(child: CircularProgressIndicator());
                  }
                  if (value.data!.isEmpty) {
                    return const Center(
                      child: Text(
                        '当前对话暂无可下载文件',
                        style: TextStyle(color: muted),
                      ),
                    );
                  }
                  return ListView(
                    children: [
                      for (final file in value.data!)
                        ListTile(
                          leading: const Icon(Icons.insert_drive_file_outlined),
                          title: Text(
                            '${file['name']}',
                            style: const TextStyle(fontSize: 14),
                          ),
                          subtitle: Text(
                            '${file['size'] ?? 0} B',
                            style: const TextStyle(fontSize: 11),
                          ),
                          trailing: IconButton(
                            tooltip: '下载文件',
                            onPressed: () async {
                              final box = c.findRenderObject() as RenderBox?;
                              final origin = box == null || !box.hasSize
                                  ? null
                                  : box.localToGlobal(Offset.zero) & box.size;
                              try {
                                await native.downloadFile(
                                  state.api,
                                  file,
                                  await state.fileUrl(file),
                                  shareOrigin: origin,
                                );
                                if (c.mounted) {
                                  ScaffoldMessenger.of(c).showSnackBar(
                                    const SnackBar(content: Text('文件已交给系统保存')),
                                  );
                                }
                              } catch (e) {
                                if (c.mounted) {
                                  ScaffoldMessenger.of(
                                    c,
                                  ).showSnackBar(SnackBar(content: Text('$e')));
                                }
                              }
                            },
                            icon: const Icon(Icons.download_outlined),
                          ),
                        ),
                    ],
                  );
                },
              ),
            ),
          ],
        ),
      ),
    ),
  );
}
