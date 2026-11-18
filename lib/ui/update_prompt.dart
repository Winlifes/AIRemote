import 'dart:async';

// ignore: deprecated_member_use
import 'package:flutter/material.dart';

import '../core/updates.dart';

class StartupUpdatePrompt extends StatefulWidget {
  final AppUpdater? updater;
  final Widget child;

  const StartupUpdatePrompt({super.key, this.updater, required this.child});

  @override
  State<StartupUpdatePrompt> createState() => _StartupUpdatePromptState();
}

class _StartupUpdatePromptState extends State<StartupUpdatePrompt>
    with WidgetsBindingObserver {
  final _shownVersions = <String>{};
  bool _dialogOpen = false;

  bool get _foreground => switch (WidgetsBinding.instance.lifecycleState) {
    null || AppLifecycleState.resumed => true,
    _ => false,
  };

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) setState(() {});
  }

  Future<void> _show(AppUpdater updater) async {
    if (!mounted || !_foreground || _dialogOpen || widget.updater != updater) {
      return;
    }
    final release = updater.automaticUpdate;
    if (release == null || !_shownVersions.add(release.version)) return;
    _dialogOpen = true;
    try {
      await showDialog<void>(
        context: context,
        builder: (dialogContext) => ListenableBuilder(
          listenable: updater,
          builder: (_, _) {
            final visible = updater.release ?? release;
            _shownVersions.add(visible.version);
            return AlertDialog(
              title: Text('发现新版本 ${visible.version}'),
              scrollable: true,
              content: Column(
                mainAxisSize: MainAxisSize.min,
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text('当前版本 ${updater.currentVersion}'),
                  if (updater.stage == 'available') ...[
                    if (visible.size case final size?)
                      Text('安装包 ${byteSize(size)}'),
                  ] else ...[
                    const SizedBox(height: 14),
                    UpdateStatus(updater: updater),
                  ],
                ],
              ),
              actions: [
                TextButton(
                  onPressed: () => Navigator.of(dialogContext).pop(),
                  child: Text(updater.stage == 'available' ? '稍后' : '关闭'),
                ),
                ?updateAction(updater),
              ],
            );
          },
        ),
      );
    } finally {
      if (mounted) setState(() => _dialogOpen = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final updater = widget.updater;
    if (updater == null) return widget.child;
    return ListenableBuilder(
      listenable: updater,
      child: widget.child,
      builder: (_, child) {
        final version = updater.automaticUpdate?.version;
        if (_foreground &&
            !_dialogOpen &&
            version != null &&
            !_shownVersions.contains(version)) {
          WidgetsBinding.instance.addPostFrameCallback(
            (_) => unawaited(_show(updater)),
          );
        }
        return child!;
      },
    );
  }
}

class UpdateStatus extends StatelessWidget {
  final AppUpdater updater;
  const UpdateStatus({super.key, required this.updater});

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    mainAxisSize: MainAxisSize.min,
    children: [
      Text(
        updater.label,
        style: const TextStyle(fontSize: 16, fontWeight: FontWeight.w500),
      ),
      if (updater.busy) ...[
        const SizedBox(height: 14),
        LinearProgressIndicator(
          value: updater.stage == 'downloading' ? updater.fraction : null,
        ),
      ],
      if (updater.stage == 'downloading')
        Padding(
          padding: const EdgeInsets.only(top: 10),
          child: Text(
            updater.detail,
            style: Theme.of(context).textTheme.bodySmall,
          ),
        ),
      if (updater.error.isNotEmpty)
        Padding(
          padding: const EdgeInsets.only(top: 12),
          child: Text(
            updater.error,
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
        ),
    ],
  );
}

Widget? updateAction(AppUpdater updater) => switch (updater.stage) {
  'available' || 'error' when updater.release != null => FilledButton(
    onPressed: updater.download,
    child: Text(updater.stage == 'error' ? '重试下载' : '下载更新'),
  ),
  'ready' || 'installing' => FilledButton(
    onPressed: updater.install,
    child: const Text('安装更新'),
  ),
  'permission' => FilledButton(
    onPressed: updater.platform.openInstallPermission,
    child: const Text('打开安装权限设置'),
  ),
  _ => null,
};
