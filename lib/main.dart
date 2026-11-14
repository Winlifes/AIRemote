// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'core/api.dart';
import 'core/platform.dart';
import 'core/updates.dart';
import 'core/workspace.dart';
import 'ui/app.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setEnabledSystemUIMode(SystemUiMode.edgeToEdge);
  SystemChrome.setSystemUIOverlayStyle(edgeStyle);
  runApp(const Bootstrap());
}

class Bootstrap extends StatefulWidget {
  const Bootstrap({super.key});
  @override
  State<Bootstrap> createState() => _BootstrapState();
}

class _BootstrapState extends State<Bootstrap> with WidgetsBindingObserver {
  final Vault vault = const SecureVault();
  final native = NativePlatform();
  Workspace? workspace;
  AppUpdater? updater;
  String? server;
  String error = '';
  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    restore();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState value) {
    if (value == AppLifecycleState.resumed) updater?.resumed();
  }

  Future<void> restore() async {
    try {
      var address = await vault.read('server');
      Json legacy = {};
      if (address == null) {
        legacy = await native.legacySession();
        address = legacy['server'] as String?;
      }
      updater = AppUpdater(vault);
      unawaitedInitialization();
      if (address != null && address.isNotEmpty) {
        await connect(address, legacyCookie: legacy['cookie'] as String?);
      } else if (mounted) {
        setState(() {
          server = '';
        });
      }
    } catch (_) {
      if (mounted) {
        setState(() {
          server = '';
          error = '无法读取本机配置，请重新填写服务器地址';
        });
      }
    }
  }

  void unawaitedInitialization() {
    updater!.initialize().catchError((Object e) {
      updater!.error = '$e';
      updater!.stage = 'error';
      updater!.changed();
    });
  }

  Future<void> connect(String address, {String? legacyCookie}) async {
    address = normalizeServer(address);
    final api = RelayApi(address, vault);
    await api.restore(legacyCookie: legacyCookie);
    await vault.write('server', address);
    workspace?.dispose();
    workspace = Workspace(api);
    if (mounted) {
      setState(() {
        server = address;
        error = '';
      });
    }
    await workspace!.start();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    workspace?.dispose();
    updater?.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AIRemoteApp(
    workspace: workspace,
    updater: updater,
    native: native,
    starting: server == null,
    setupError: error,
    connect: connect,
    changeServer: () => setState(() {
      workspace?.dispose();
      workspace = null;
      server = '';
    }),
  );
}
