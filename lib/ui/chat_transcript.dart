import 'dart:async';
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollDirection;
import 'package:flutter/services.dart';
import 'package:flutter_markdown_plus/flutter_markdown_plus.dart';
import 'package:markdown/markdown.dart' as md;
import '../core/api.dart';
import '../core/workspace.dart';
import 'app.dart';

const _transcriptTint = Color(0xfff5f3fa);
const _transcriptBorder = Color(0xffeae6f2);
const _promptColor = Color(0xff30284a);

bool _isProgress(Json message) =>
    message['role'] == 'assistant' && message['phase'] == 'commentary';

List<JsonList> _entries(JsonList messages) {
  final entries = <JsonList>[];
  for (final message in messages) {
    final previous = entries.lastOrNull?.last;
    if (previous != null &&
        _isProgress(previous) &&
        _isProgress(message) &&
        previous['turnId'] == message['turnId']) {
      entries.last.add(message);
    } else {
      entries.add([message]);
    }
  }
  return entries;
}

DateTime? _messageTime(Json message) {
  final value = message['createdAtMs'];
  return value is num && value > 0 && value <= 8640000000000000
      ? DateTime.fromMillisecondsSinceEpoch(value.toInt())
      : null;
}

String _clock(BuildContext context, DateTime date) => MaterialLocalizations.of(
  context,
).formatTimeOfDay(TimeOfDay.fromDateTime(date), alwaysUse24HourFormat: true);

String _dateLabel(DateTime date) {
  final today = DateUtils.dateOnly(DateTime.now());
  final yesterday = DateTime(today.year, today.month, today.day - 1);
  return DateUtils.isSameDay(today, date)
      ? '今天'
      : DateUtils.isSameDay(yesterday, date)
      ? '昨天'
      : date.year == today.year
      ? '${date.month}月${date.day}日'
      : '${date.year}年${date.month}月${date.day}日';
}

class ChatTranscript extends StatelessWidget {
  final Workspace state;
  final ScrollController scroll;
  final ValueChanged<String> link, reuse;
  const ChatTranscript({
    super.key,
    required this.state,
    required this.scroll,
    required this.link,
    required this.reuse,
  });

  @override
  Widget build(BuildContext context) {
    if (state.messages.isEmpty) {
      return Center(
        child: state.loading && state.online
            ? const CircularProgressIndicator(
                strokeWidth: 2,
                semanticsLabel: '正在读取对话',
              )
            : Text(
                state.online ? '这条对话还没有消息' : '连接恢复后加载消息',
                style: const TextStyle(color: muted),
              ),
      );
    }
    final entries = _entries(state.messages).reversed.toList(growable: false);
    final showPaging = state.hasOlder || state.loadingOlder;
    final indices = <String, int>{
      for (var i = 0; i < entries.length; i++)
        'message-${entries[i].first['itemId']}': i,
      if (showPaging) 'history-page': entries.length,
    };
    final turns = {
      for (final turn in objects(state.chat?['turns'])) turn['turn_id']: turn,
    };
    final liveTurn = state.isRunning ? turns.keys.firstOrNull : null;
    final firstReplies = <Object?, Object?>{};
    for (final message in state.messages) {
      if (message['role'] == 'assistant' && !_isProgress(message)) {
        firstReplies.putIfAbsent(message['turnId'], () => message['itemId']);
      }
    }
    final thread = state.draftKey;
    return Stack(
      fit: StackFit.expand,
      children: [
        Align(
          alignment: Alignment.topCenter,
          child: NotificationListener<ScrollNotification>(
            onNotification: (event) {
              if (event.depth != 0) return false;
              if (event is ScrollUpdateNotification ||
                  event is ScrollEndNotification) {
                final follow = event.metrics.extentBefore < 72;
                if (!state.loadingOlder && state.followLatest != follow) {
                  WidgetsBinding.instance.addPostFrameCallback((_) {
                    if (state.disposed ||
                        thread != state.draftKey ||
                        !scroll.hasClients ||
                        state.loadingOlder) {
                      return;
                    }
                    final atLatest = scroll.position.extentBefore < 72;
                    final pending = atLatest && state.hasNewMessages;
                    state.setFollowingLatest(atLatest);
                    if (pending) scroll.jumpTo(0);
                  });
                }
              }
              if (event is UserScrollNotification &&
                  event.direction == ScrollDirection.reverse &&
                  event.metrics.extentAfter < 100 &&
                  state.hasOlder) {
                WidgetsBinding.instance.addPostFrameCallback((_) {
                  if (!state.disposed && thread == state.draftKey) {
                    unawaited(state.loadOlder());
                  }
                });
              }
              return false;
            },
            child: ListView.builder(
              key: ValueKey(thread),
              controller: scroll,
              reverse: true,
              // Short conversations start below the header. Longer histories use
              // the lazy, bottom-anchored viewport.
              shrinkWrap: entries.length <= 12,
              padding: const EdgeInsets.fromLTRB(18, 8, 18, 12),
              itemCount: entries.length + (showPaging ? 1 : 0),
              findChildIndexCallback: (key) =>
                  key is ValueKey<String> ? indices[key.value] : null,
              itemBuilder: (context, index) {
                if (index == entries.length) {
                  return Center(
                    key: const ValueKey('history-page'),
                    child: TextButton.icon(
                      onPressed: state.loadingOlder || !state.online
                          ? null
                          : state.loadOlder,
                      icon: state.loadingOlder
                          ? const SizedBox.square(
                              dimension: 12,
                              child: CircularProgressIndicator(
                                strokeWidth: 1.5,
                              ),
                            )
                          : const Icon(
                              Icons.keyboard_arrow_up_rounded,
                              size: 18,
                            ),
                      label: Text(state.loadingOlder ? '正在读取更早消息…' : '加载更早消息'),
                    ),
                  );
                }
                final entry = entries[index], first = entry.first;
                final time = _messageTime(first);
                final previous = index + 1 < entries.length
                    ? _messageTime(entries[index + 1].last)
                    : null;
                final progress = _isProgress(first);
                final turn = turns[first['turnId']];
                final started = turn?['started_at'],
                    finished = turn?['completed_at'];
                final duration =
                    !progress &&
                        first['role'] == 'assistant' &&
                        firstReplies[first['turnId']] == first['itemId'] &&
                        started is num &&
                        finished is num &&
                        finished >= started
                    ? finished - started
                    : null;
                return Column(
                  key: ValueKey('message-${first['itemId']}'),
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    if (time != null && !DateUtils.isSameDay(time, previous))
                      Padding(
                        padding: const EdgeInsets.symmetric(vertical: 15),
                        child: Text(
                          _dateLabel(time),
                          textAlign: TextAlign.center,
                          style: const TextStyle(fontSize: 11, color: muted),
                        ),
                      ),
                    if (progress)
                      _ProgressSummary(
                        entry: entry,
                        live:
                            index == 0 &&
                            liveTurn != null &&
                            first['turnId'] == liveTurn,
                        preview: index == 0,
                        open: () => _inspectProgress(context, entry),
                      )
                    else if (first['role'] == 'user')
                      _Prompt(message: first, time: time, reuse: reuse)
                    else
                      Padding(
                        padding: const EdgeInsets.only(top: 10, bottom: 14),
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            _ReplyMarkdown(
                              text: '${first['text'] ?? ''}',
                              link: link,
                            ),
                            Row(
                              children: [
                                _CopyAction(
                                  label: '复制回复',
                                  text: '${first['text'] ?? ''}',
                                ),
                                if (duration != null)
                                  Flexible(
                                    child: Text(
                                      '已处理 ${durationLabel(duration)}',
                                      style: const TextStyle(
                                        fontSize: 12,
                                        color: muted,
                                      ),
                                    ),
                                  ),
                              ],
                            ),
                          ],
                        ),
                      ),
                  ],
                );
              },
            ),
          ),
        ),
        if (!state.followLatest)
          Positioned(
            left: 8,
            right: 8,
            bottom: 7,
            child: Center(
              child: FilledButton.tonalIcon(
                style: FilledButton.styleFrom(
                  elevation: 3,
                  backgroundColor: state.hasNewMessages
                      ? _promptColor
                      : Colors.white,
                  foregroundColor: state.hasNewMessages ? Colors.white : ink,
                  minimumSize: const Size(0, 44),
                ),
                onPressed: () {
                  state.jumpLatest();
                  if (scroll.hasClients) scroll.jumpTo(0);
                },
                icon: const Icon(Icons.south_rounded, size: 16),
                label: Text(state.hasNewMessages ? '有更新 · 回到最新' : '回到最新'),
              ),
            ),
          ),
      ],
    );
  }

  void _inspectProgress(BuildContext context, JsonList original) {
    final thread = state.draftKey;
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      useSafeArea: true,
      showDragHandle: true,
      constraints: const BoxConstraints(maxWidth: 640),
      backgroundColor: Colors.white,
      builder: (context) => ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: MediaQuery.sizeOf(context).height * .76,
        ),
        child: ListenableBuilder(
          listenable: state,
          builder: (context, _) {
            final entry = state.authenticated && state.draftKey == thread
                ? _entries(state.messages)
                          .where(
                            (e) =>
                                e.first['itemId'] == original.first['itemId'],
                          )
                          .firstOrNull ??
                      original
                : <Json>[];
            return Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 0, 10, 7),
                  child: Row(
                    children: [
                      Expanded(
                        child: Text(
                          '执行过程 · ${entry.length} 条',
                          style: Theme.of(context).textTheme.titleMedium,
                        ),
                      ),
                      IconButton(
                        tooltip: '关闭执行过程',
                        onPressed: () => Navigator.pop(context),
                        icon: const Icon(Icons.close_rounded),
                      ),
                    ],
                  ),
                ),
                const Divider(height: 1, color: _transcriptBorder),
                Flexible(
                  child: SafeArea(
                    top: false,
                    child: ListView.separated(
                      shrinkWrap: entry.length <= 12,
                      padding: const EdgeInsets.fromLTRB(20, 15, 20, 20),
                      itemCount: entry.length,
                      separatorBuilder: (_, _) =>
                          const Divider(height: 25, color: _transcriptBorder),
                      itemBuilder: (context, index) {
                        final note = entry[index], time = _messageTime(note);
                        return Column(
                          key: ValueKey(note['itemId']),
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            if (time != null)
                              Text(
                                _clock(context, time),
                                style: const TextStyle(
                                  fontSize: 11,
                                  color: muted,
                                ),
                              ),
                            _ReplyMarkdown(
                              text: '${note['text'] ?? ''}',
                              link: link,
                              compact: true,
                            ),
                          ],
                        );
                      },
                    ),
                  ),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}

class _Prompt extends StatelessWidget {
  final Json message;
  final DateTime? time;
  final ValueChanged<String> reuse;
  const _Prompt({
    required this.message,
    required this.time,
    required this.reuse,
  });

  @override
  Widget build(BuildContext context) {
    final text = userText('${message['text'] ?? ''}');
    return Align(
      alignment: Alignment.centerRight,
      child: LayoutBuilder(
        builder: (context, space) => ConstrainedBox(
          constraints: BoxConstraints(maxWidth: space.maxWidth * .88),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Container(
                padding: const EdgeInsets.symmetric(
                  horizontal: 15,
                  vertical: 11,
                ),
                decoration: const BoxDecoration(
                  color: _promptColor,
                  borderRadius: BorderRadius.only(
                    topLeft: Radius.circular(18),
                    topRight: Radius.circular(18),
                    bottomLeft: Radius.circular(18),
                    bottomRight: Radius.circular(6),
                  ),
                ),
                child: SelectableText(
                  text,
                  style: const TextStyle(
                    color: Colors.white,
                    fontSize: 15,
                    height: 1.55,
                  ),
                ),
              ),
              if (text.trim().isNotEmpty)
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (time != null)
                      Text(
                        _clock(context, time!),
                        style: const TextStyle(fontSize: 11, color: muted),
                      ),
                    IconButton(
                      tooltip: '放入输入框',
                      onPressed: () => reuse(text),
                      icon: const Icon(
                        Icons.edit_outlined,
                        size: 15,
                        color: muted,
                      ),
                    ),
                  ],
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _ProgressSummary extends StatelessWidget {
  final JsonList entry;
  final bool live, preview;
  final VoidCallback open;
  const _ProgressSummary({
    required this.entry,
    required this.live,
    required this.preview,
    required this.open,
  });

  @override
  Widget build(BuildContext context) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 5),
    child: Material(
      color: _transcriptTint,
      shape: RoundedRectangleBorder(
        borderRadius: BorderRadius.circular(13),
        side: const BorderSide(color: _transcriptBorder),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(13),
        onTap: open,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(12, 10, 10, 10),
          child: Row(
            children: [
              Icon(
                live ? Icons.bolt_outlined : Icons.notes_rounded,
                size: 20,
                color: const Color(0xff655389),
              ),
              const SizedBox(width: 9),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(
                      live ? '实时进度' : '执行过程',
                      style: const TextStyle(
                        fontSize: 13,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                    if (preview)
                      Text(
                        '${entry.last['text'] ?? ''}'
                            .replaceAll(RegExp(r'\s+'), ' ')
                            .trim(),
                        maxLines: 1,
                        overflow: TextOverflow.ellipsis,
                        style: const TextStyle(fontSize: 12, color: muted),
                      ),
                  ],
                ),
              ),
              Text(
                '${entry.length} 条',
                style: const TextStyle(fontSize: 11, color: muted),
              ),
              const SizedBox(width: 5),
              const Icon(Icons.chevron_right_rounded, size: 18, color: muted),
            ],
          ),
        ),
      ),
    ),
  );
}

class _ReplyMarkdown extends StatelessWidget {
  final String text;
  final ValueChanged<String> link;
  final bool compact;
  const _ReplyMarkdown({
    required this.text,
    required this.link,
    this.compact = false,
  });

  @override
  Widget build(BuildContext context) => MarkdownBody(
    data: text,
    selectable: true,
    builders: {'pre': _CodeBlockBuilder()},
    styleSheet: MarkdownStyleSheet.fromTheme(Theme.of(context)).copyWith(
      p: TextStyle(fontSize: compact ? 14 : 15.5, height: 1.65, color: ink),
      code: const TextStyle(fontSize: 13, fontFamily: 'monospace'),
      codeblockDecoration: const BoxDecoration(),
      a: const TextStyle(color: Color(0xff5743a6)),
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
  );
}

class _CodeBlockBuilder extends MarkdownElementBuilder {
  @override
  Widget visitElementAfterWithContext(
    BuildContext context,
    md.Element element,
    TextStyle? preferredStyle,
    TextStyle? parentStyle,
  ) {
    final code = element.children?.whereType<md.Element>().firstOrNull;
    final language = code?.attributes['class']?.split(' ').first;
    final raw = element.textContent;
    final text = raw.endsWith('\n') ? raw.substring(0, raw.length - 1) : raw;
    return Container(
      width: double.infinity,
      decoration: BoxDecoration(
        color: _transcriptTint,
        borderRadius: BorderRadius.circular(13),
        border: Border.all(color: _transcriptBorder),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.only(left: 12, right: 5),
            child: Row(
              children: [
                Expanded(
                  child: Text(
                    language?.startsWith('language-') == true
                        ? language!.substring(9)
                        : '代码',
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 12,
                      fontFamily: 'monospace',
                      color: muted,
                    ),
                  ),
                ),
                _CopyAction(label: '复制代码', text: text),
              ],
            ),
          ),
          const Divider(height: 1, color: _transcriptBorder),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            primary: false,
            padding: const EdgeInsets.fromLTRB(12, 10, 12, 12),
            child: SelectableText(
              text,
              style: const TextStyle(
                fontSize: 13,
                height: 1.55,
                fontFamily: 'monospace',
                color: ink,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _CopyAction extends StatefulWidget {
  final String label, text;
  const _CopyAction({required this.label, required this.text});

  @override
  State<_CopyAction> createState() => _CopyActionState();
}

class _CopyActionState extends State<_CopyAction> {
  Timer? _reset;
  bool copied = false;
  @override
  void dispose() {
    _reset?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => TextButton.icon(
    onPressed: () async {
      try {
        await Clipboard.setData(ClipboardData(text: widget.text));
        if (!mounted) return;
        setState(() => copied = true);
        _reset?.cancel();
        _reset = Timer(
          const Duration(seconds: 2),
          () => setState(() => copied = false),
        );
      } on PlatformException {
        if (context.mounted) {
          ScaffoldMessenger.of(
            context,
          ).showSnackBar(const SnackBar(content: Text('可长按文字复制')));
        }
      }
    },
    style: TextButton.styleFrom(
      foregroundColor: muted,
      minimumSize: const Size(44, 44),
      padding: const EdgeInsets.symmetric(horizontal: 8),
    ),
    icon: Icon(copied ? Icons.check_rounded : Icons.copy_outlined, size: 15),
    label: Text(
      copied ? '已复制' : widget.label,
      style: const TextStyle(fontSize: 12),
    ),
  );
}
