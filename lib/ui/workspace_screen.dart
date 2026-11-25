import 'dart:math';
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';
import '../core/api.dart';
import '../core/platform.dart';
import '../core/workspace.dart';
import 'app.dart';
import 'chat_transcript.dart';
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
  final composeFocus = FocusNode(debugLabel: "chat-composer");
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
    composeFocus.dispose();
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
            _header(padding.top, selected),
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
                          ? ChatTranscript(
                              state: state,
                              scroll: scroll,
                              link: (path) => openMessageLink(
                                context,
                                state,
                                widget.native,
                                path,
                              ),
                              reuse: _reusePrompt,
                            )
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
                                      onPressed: state.canCreate
                                          ? () => showNewChat(context, state)
                                          : null,
                                      child: const Text('新建对话'),
                                    ),
                                  ],
                                ),
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
                            focus: composeFocus,
                            onSent: _showLatest,
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

  void _showLatest() {
    if (scroll.hasClients) scroll.jumpTo(0);
  }

  void _reusePrompt(String text) {
    final saved = state.draft;
    final draft = saved.isEmpty ? text : '$saved\n\n$text';
    if (draft.length > 120000) {
      ScaffoldMessenger.of(
        context,
      ).showSnackBar(const SnackBar(content: Text('输入框内容已接近上限')));
      return;
    }
    state.setDraft(draft);
    composeFocus.requestFocus();
  }

  Widget _header(double safeTop, bool selected) {
    final chat = state.chat;
    final folder = chat?['cwd']
        ?.toString()
        .split(RegExp(r'[/\\]'))
        .where((part) => part.isNotEmpty)
        .lastOrNull;
    final status = state.device['online'] != true
        ? '电脑离线'
        : state.device['connectorOnline'] != true
        ? '连接器离线'
        : state.hasLiveStatus
        ? runtimeLabel(chat)
        : '状态待同步';
    final statusColor = switch (status) {
      '运行中' => const Color(0xff257657),
      '等待处理' || '电脑离线' || '连接器离线' => const Color(0xff97561f),
      _ => muted,
    };
    return Container(
      decoration: selected
          ? const BoxDecoration(
              border: Border(bottom: BorderSide(color: Color(0xffeae7f0))),
            )
          : null,
      padding: EdgeInsets.fromLTRB(8, safeTop + 3, 7, 3),
      constraints: BoxConstraints(minHeight: safeTop + 62),
      child: Row(
        children: [
          IconButton(
            tooltip: '打开项目与对话',
            onPressed: openDrawer,
            icon: const Icon(Icons.menu_rounded),
          ),
          Expanded(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 7),
              child: selected
                  ? Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(
                          chat?['title']?.toString() ?? '正在读取对话…',
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontSize: 16,
                            fontWeight: FontWeight.w600,
                            height: 1.3,
                          ),
                        ),
                        const SizedBox(height: 3),
                        LayoutBuilder(
                          builder: (context, space) => Row(
                            children: [
                              ProviderIcon(state.provider, 13),
                              const SizedBox(width: 5),
                              Flexible(
                                child: Text(
                                  folder == null
                                      ? state.providerName
                                      : '${state.providerName} / $folder',
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: const TextStyle(
                                    fontSize: 11,
                                    color: muted,
                                    height: 1.25,
                                  ),
                                ),
                              ),
                              const SizedBox(width: 8),
                              ExcludeSemantics(
                                child: SizedBox.square(
                                  dimension: 6,
                                  child: DecoratedBox(
                                    decoration: BoxDecoration(
                                      color: statusColor,
                                      shape: BoxShape.circle,
                                    ),
                                  ),
                                ),
                              ),
                              const SizedBox(width: 5),
                              ConstrainedBox(
                                constraints: BoxConstraints(
                                  maxWidth: space.maxWidth * .46,
                                ),
                                child: Text(
                                  status,
                                  maxLines: 1,
                                  overflow: TextOverflow.ellipsis,
                                  style: TextStyle(
                                    fontSize: 11,
                                    color: statusColor,
                                    fontWeight: FontWeight.w600,
                                    height: 1.25,
                                  ),
                                ),
                              ),
                            ],
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
          if (selected) ...[
            IconButton(
              tooltip: '对话文件',
              onPressed: () => showFiles(context, state, widget.native),
              icon: const Icon(Icons.folder_outlined, size: 21),
            ),
            PopupMenuButton<_ChatCommand>(
              tooltip: '对话选项',
              icon: const Icon(Icons.more_horiz, size: 22),
              position: PopupMenuPosition.under,
              itemBuilder: (_) => [
                PopupMenuItem(
                  value: _ChatCommand.refresh,
                  enabled: state.online && !state.loading,
                  child: const Text('刷新对话'),
                ),
                if (state.canSetGoal && state.goal == null)
                  const PopupMenuItem(
                    value: _ChatCommand.goal,
                    child: Text('对话目标'),
                  ),
                const PopupMenuItem(
                  value: _ChatCommand.settings,
                  child: Text('应用设置与更新'),
                ),
              ],
              onSelected: (action) {
                switch (action) {
                  case _ChatCommand.refresh:
                    state.refreshChat();
                  case _ChatCommand.goal:
                    showGoal(context, state);
                  case _ChatCommand.settings:
                    widget.settings();
                }
              },
            ),
          ] else
            IconButton(
              tooltip: '应用设置与更新',
              onPressed: widget.settings,
              icon: const Icon(Icons.more_horiz, size: 22),
            ),
        ],
      ),
    );
  }
}

enum _ChatCommand { refresh, goal, settings }

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
    final query = state.search.toLowerCase();
    final projects = state.projects.where(
      (project) => '${project['name']}'.toLowerCase().contains(query),
    );
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
                for (final provider in providerNames.keys)
                  Padding(
                    padding: const EdgeInsets.only(right: 9),
                    child: Semantics(
                      label: '切换到 ${providerNames[provider]}',
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
                                  [
                                    state.device['online'] == true
                                        ? item['statusLabel'] ?? '检查中'
                                        : 'Mac 离线',
                                    if (item['version'] != null)
                                      item['version'],
                                    if (item['message'] != null)
                                      item['message'],
                                  ].join('\n'),
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
                              ProviderIcon(provider, 26),
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
                  tooltip: state.loadingProjectList && state.projects.isNotEmpty
                      ? '正在刷新项目列表'
                      : '刷新项目列表',
                  onPressed: state.online && !state.loadingProjectList
                      ? state.refreshProjectTree
                      : null,
                  icon: state.loadingProjectList && state.projects.isNotEmpty
                      ? const SizedBox.square(
                          dimension: 16,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.refresh, size: 20),
                ),
                IconButton(
                  tooltip: '新建对话',
                  onPressed: state.canCreate ? widget.newChat : null,
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
                if (state.projectListFailed)
                  ListTile(
                    title: const Text('项目加载失败'),
                    trailing: TextButton(
                      onPressed: state.online ? state.refreshProjectTree : null,
                      child: const Text('重试'),
                    ),
                  ),
                if (state.loadingProjectList && state.projects.isEmpty)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 36),
                    child: Semantics(
                      liveRegion: true,
                      child: const Column(
                        children: [
                          SizedBox.square(
                            dimension: 20,
                            child: CircularProgressIndicator(strokeWidth: 2),
                          ),
                          SizedBox(height: 12),
                          Text('正在加载项目…', style: TextStyle(color: muted)),
                        ],
                      ),
                    ),
                  )
                else if (projects.isEmpty && !state.projectListFailed)
                  Padding(
                    padding: const EdgeInsets.symmetric(vertical: 36),
                    child: Text(
                      state.projects.isNotEmpty
                          ? '没有匹配的项目'
                          : state.online
                          ? '这里还没有项目'
                          : '等待电脑连接',
                      textAlign: TextAlign.center,
                      style: const TextStyle(color: muted),
                    ),
                  ),
                for (final project in projects) ...[
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
