// Keep the same Material types as flutter_markdown_plus until its migration.
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import '../core/platform.dart';
import '../core/updates.dart';
import '../core/workspace.dart';
import 'workspace_screen.dart';

const edgeStyle = SystemUiOverlayStyle(
  statusBarColor: Colors.transparent,
  statusBarIconBrightness: Brightness.dark,
  statusBarBrightness: Brightness.light,
  systemNavigationBarColor: Colors.transparent,
  systemNavigationBarDividerColor: Colors.transparent,
  systemNavigationBarIconBrightness: Brightness.dark,
  systemStatusBarContrastEnforced: false,
  systemNavigationBarContrastEnforced: false,
);
const ink = Color(0xff252631),
    muted = Color(0xff8b8c98),
    accent = Color(0xff675cf4),
    canvas = Color(0xfffafafa);

class AIRemoteApp extends StatelessWidget {
  final Workspace? workspace;
  final AppUpdater? updater;
  final NativePlatform native;
  final bool starting;
  final String setupError;
  final Future<void> Function(String) connect;
  final VoidCallback changeServer;
  const AIRemoteApp({
    super.key,
    required this.workspace,
    required this.updater,
    required this.native,
    required this.connect,
    required this.changeServer,
    this.starting = false,
    this.setupError = '',
  });
  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'AIRemote',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(
        seedColor: accent,
        surface: Colors.white,
      ),
      scaffoldBackgroundColor: canvas,
      splashFactory: InkRipple.splashFactory,
      appBarTheme: const AppBarTheme(
        backgroundColor: canvas,
        surfaceTintColor: Colors.transparent,
        elevation: 0,
      ),
      inputDecorationTheme: InputDecorationTheme(
        filled: true,
        fillColor: const Color(0xfff5f5f7),
        border: OutlineInputBorder(
          borderRadius: BorderRadius.circular(16),
          borderSide: BorderSide.none,
        ),
      ),
      textTheme: const TextTheme(
        bodyMedium: TextStyle(fontSize: 15, color: ink, height: 1.6),
      ),
    ),
    home: AnnotatedRegion<SystemUiOverlayStyle>(
      value: edgeStyle,
      child: Builder(
        builder: (context) {
          if (starting) {
            return const Scaffold(
              body: Center(child: CircularProgressIndicator()),
            );
          }
          final state = workspace;
          if (state == null) {
            return ServerSetup(connect: connect, error: setupError);
          }
          return ListenableBuilder(
            listenable: state,
            builder: (_, _) {
              if (state.booting) {
                return const Scaffold(
                  body: Center(child: CircularProgressIndicator()),
                );
              }
              if (!state.authenticated) {
                return LoginScreen(state: state, changeServer: changeServer);
              }
              return WorkspaceScreen(
                state: state,
                native: native,
                settings: () =>
                    showSettings(context, updater, state, changeServer),
              );
            },
          );
        },
      ),
    ),
  );
}

class Brand extends StatelessWidget {
  final double size;
  const Brand({super.key, this.size = 30});
  @override
  Widget build(BuildContext context) => Row(
    mainAxisSize: MainAxisSize.min,
    children: [
      ClipRRect(
        borderRadius: BorderRadius.circular(size * .26),
        child: Image.asset('assets/airemote.png', width: size, height: size),
      ),
      const SizedBox(width: 10),
      Text(
        'AIRemote.',
        style: TextStyle(
          fontWeight: FontWeight.w700,
          fontSize: size * .73,
          letterSpacing: -.6,
          color: ink,
        ),
      ),
    ],
  );
}

class ServerSetup extends StatefulWidget {
  final Future<void> Function(String) connect;
  final String error;
  const ServerSetup({super.key, required this.connect, this.error = ''});
  @override
  State<ServerSetup> createState() => _ServerSetupState();
}

class _ServerSetupState extends State<ServerSetup> {
  final address = TextEditingController();
  bool busy = false;
  String error = '';
  @override
  void dispose() {
    address.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    body: SafeArea(
      child: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(28),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Brand(size: 46),
                const SizedBox(height: 38),
                const Text(
                  '你的 AI，随时继续。',
                  style: TextStyle(fontSize: 27, fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 12),
                const Text(
                  '填写你的 AIRemote 服务器地址，连接 Mac 上的对话。',
                  style: TextStyle(color: muted),
                ),
                const SizedBox(height: 26),
                TextField(
                  controller: address,
                  keyboardType: TextInputType.url,
                  autocorrect: false,
                  decoration: const InputDecoration(
                    labelText: '服务器地址',
                    hintText: 'https://remote.example.com',
                  ),
                ),
                if (error.isNotEmpty || widget.error.isNotEmpty)
                  Padding(
                    padding: const EdgeInsets.only(top: 12),
                    child: Text(
                      error.isNotEmpty ? error : widget.error,
                      style: const TextStyle(color: Colors.red),
                    ),
                  ),
                const SizedBox(height: 20),
                SizedBox(
                  width: double.infinity,
                  child: FilledButton(
                    onPressed: busy
                        ? null
                        : () async {
                            setState(() {
                              busy = true;
                              error = '';
                            });
                            try {
                              await widget.connect(address.text);
                            } catch (e) {
                              if (mounted) {
                                setState(() {
                                  error = '$e';
                                });
                              }
                            } finally {
                              if (mounted) {
                                setState(() {
                                  busy = false;
                                });
                              }
                            }
                          },
                    child: Padding(
                      padding: const EdgeInsets.all(13),
                      child: busy
                          ? const SizedBox.square(
                              dimension: 20,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('连接服务器'),
                    ),
                  ),
                ),
              ],
            ),
          ),
        ),
      ),
    ),
  );
}

class LoginScreen extends StatefulWidget {
  final Workspace state;
  final VoidCallback changeServer;
  const LoginScreen({
    super.key,
    required this.state,
    required this.changeServer,
  });
  @override
  State<LoginScreen> createState() => _LoginScreenState();
}

class _LoginScreenState extends State<LoginScreen> {
  final username = TextEditingController(), password = TextEditingController();
  @override
  void dispose() {
    username.dispose();
    password.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
    body: SafeArea(
      child: Center(
        child: SingleChildScrollView(
          padding: const EdgeInsets.all(28),
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 420),
            child: AutofillGroup(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Brand(size: 42),
                  const SizedBox(height: 30),
                  const Text(
                    '连接你的工作空间',
                    style: TextStyle(fontSize: 25, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 9),
                  Text(
                    widget.state.api.server,
                    style: const TextStyle(fontSize: 12, color: muted),
                  ),
                  const SizedBox(height: 24),
                  TextField(
                    controller: username,
                    autofillHints: const [AutofillHints.username],
                    decoration: const InputDecoration(labelText: '账号'),
                  ),
                  const SizedBox(height: 16),
                  TextField(
                    controller: password,
                    obscureText: true,
                    autofillHints: const [AutofillHints.password],
                    decoration: const InputDecoration(labelText: '访问密码'),
                  ),
                  if (widget.state.error.isNotEmpty)
                    Padding(
                      padding: const EdgeInsets.only(top: 12),
                      child: Text(
                        widget.state.error,
                        style: const TextStyle(color: Colors.red),
                      ),
                    ),
                  const SizedBox(height: 22),
                  SizedBox(
                    width: double.infinity,
                    child: FilledButton(
                      onPressed: widget.state.busy
                          ? null
                          : () async {
                              await widget.state.login(
                                username.text.trim(),
                                password.text,
                              );
                              if (widget.state.authenticated) {
                                password.clear();
                                TextInput.finishAutofillContext();
                              }
                            },
                      child: const Padding(
                        padding: EdgeInsets.all(13),
                        child: Text('登录'),
                      ),
                    ),
                  ),
                  TextButton(
                    onPressed: widget.changeServer,
                    child: const Text('更换服务器'),
                  ),
                ],
              ),
            ),
          ),
        ),
      ),
    ),
  );
}

void showSettings(
  BuildContext context,
  AppUpdater? updater,
  Workspace state,
  VoidCallback changeServer,
) {
  showModalBottomSheet<void>(
    context: context,
    useSafeArea: true,
    isScrollControlled: true,
    showDragHandle: true,
    builder: (context) => SafeArea(
      top: false,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(22, 0, 22, 20),
        child: updater == null
            ? const Text('应用设置')
            : ListenableBuilder(
                listenable: updater,
                builder: (_, _) => Column(
                  mainAxisSize: MainAxisSize.min,
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        const Text(
                          '应用设置',
                          style: TextStyle(
                            fontSize: 21,
                            fontWeight: FontWeight.w600,
                          ),
                        ),
                        const Spacer(),
                        Text(
                          updater.currentVersion,
                          style: const TextStyle(color: muted),
                        ),
                      ],
                    ),
                    const SizedBox(height: 12),
                    ListTile(
                      contentPadding: EdgeInsets.zero,
                      leading: const Icon(Icons.dns_outlined),
                      title: const Text('服务器地址'),
                      subtitle: Text(state.api.server),
                      onTap: () {
                        Navigator.pop(context);
                        changeServer();
                      },
                    ),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      title: const Text('自动检查更新'),
                      value: updater.autoCheck,
                      onChanged: updater.setAuto,
                    ),
                    const Divider(),
                    const SizedBox(height: 12),
                    Text(
                      updater.label,
                      style: const TextStyle(
                        fontSize: 16,
                        fontWeight: FontWeight.w500,
                      ),
                    ),
                    if (updater.busy) ...[
                      const SizedBox(height: 14),
                      LinearProgressIndicator(
                        value: updater.stage == 'downloading'
                            ? updater.fraction
                            : null,
                      ),
                    ],
                    if (updater.stage == 'downloading')
                      Padding(
                        padding: const EdgeInsets.only(top: 10),
                        child: Text(
                          updater.detail,
                          style: const TextStyle(color: muted, fontSize: 12),
                        ),
                      ),
                    if (updater.error.isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 12),
                        child: Text(
                          updater.error,
                          style: const TextStyle(
                            color: Colors.red,
                            fontSize: 13,
                          ),
                        ),
                      ),
                    const SizedBox(height: 16),
                    Wrap(
                      spacing: 10,
                      runSpacing: 8,
                      children: [
                        if (!updater.busy)
                          OutlinedButton(
                            onPressed: updater.check,
                            child: const Text('检查更新'),
                          ),
                        if (updater.stage == 'available' ||
                            updater.stage == 'error' && updater.release != null)
                          FilledButton(
                            onPressed: updater.download,
                            child: Text(
                              updater.stage == 'error' ? '重试下载' : '下载更新',
                            ),
                          ),
                        if (['ready', 'installing'].contains(updater.stage))
                          FilledButton(
                            onPressed: updater.install,
                            child: const Text('安装更新'),
                          ),
                        if (updater.stage == 'permission')
                          FilledButton(
                            onPressed: () async {
                              await updater.platform.openInstallPermission();
                            },
                            child: const Text('打开安装权限设置'),
                          ),
                        TextButton(
                          onPressed: () => launchUrl(
                            Uri.parse('https://github.com/Winlifes/AIRemote'),
                            mode: LaunchMode.externalApplication,
                          ),
                          child: const Text('开源仓库'),
                        ),
                      ],
                    ),
                    if (updater.busy)
                      const Padding(
                        padding: EdgeInsets.only(top: 10),
                        child: Text(
                          '关闭面板后下载继续，再次打开设置可查看进度。',
                          style: TextStyle(fontSize: 12, color: muted),
                        ),
                      ),
                  ],
                ),
              ),
      ),
    ),
  );
}
