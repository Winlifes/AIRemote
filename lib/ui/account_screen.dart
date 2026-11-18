import 'dart:async';

// Keep the same Material types as flutter_markdown_plus until its migration.
// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../core/api.dart';
import '../core/workspace.dart';

class AccountScreen extends StatefulWidget {
  final Workspace state;
  final bool admin;

  const AccountScreen({super.key, required this.state, this.admin = false});

  @override
  State<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends State<AccountScreen> {
  final _inviteName = TextEditingController();
  late final Timer _statusTimer;
  Json? _data, _issued;
  String _error = '';
  bool _busy = false;

  String get _path => widget.admin ? '/api/admin' : '/api/account';
  String get _title => widget.admin ? '管理员' : '账号与设备';
  bool get _active =>
      mounted && !widget.state.disposed && widget.state.authenticated;

  @override
  void initState() {
    super.initState();
    unawaited(_update());
    _statusTimer = Timer.periodic(const Duration(seconds: 6), (_) {
      if (widget.state.visible && ModalRoute.isCurrentOf(context) == true) {
        unawaited(_update(background: true));
      }
    });
  }

  @override
  void dispose() {
    _statusTimer.cancel();
    _inviteName.dispose();
    super.dispose();
  }

  Future<void> _update({
    Future<void> Function()? change,
    bool background = false,
  }) async {
    if (!_active || _busy) return;
    setState(() {
      _busy = true;
      if (!background) _error = '';
    });
    try {
      if (change != null) await change();
      if (!_active) return;
      final value = await widget.state.accountRequest(_path);
      if (!_active) return;
      setState(() {
        if (_data == null) _error = '';
        _data = value;
        if (widget.admin &&
            _issued != null &&
            !objects(
              value['invitations'],
            ).any((item) => item['id'] == _issued!['id'])) {
          _issued = null;
        }
      });
    } catch (e) {
      if (_active && (!background || _data == null)) {
        setState(() => _error = '$e');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<Json> _post(String suffix, [Json body = const {}]) =>
      widget.state.accountRequest('$_path$suffix', method: 'POST', body: body);

  Future<void> _generateCode() => _update(
    change: () async {
      final value = widget.admin
          ? await _post('/invitations', {'username': _inviteName.text.trim()})
          : await _post('/pairings');
      if (!_active) return;
      if (value['code'] is! String || value['expiresAt'] is! num) {
        throw const ApiError('服务器没有返回可用的验证码。');
      }
      setState(() => _issued = value);
      _inviteName.clear();
    },
  );

  Future<bool> _confirm(String title, String message, String action) async =>
      await showDialog<bool>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(title),
          content: Text(message),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context, false),
              child: const Text('取消'),
            ),
            TextButton(
              onPressed: () => Navigator.pop(context, true),
              child: Text(action),
            ),
          ],
        ),
      ) ==
      true;

  Future<void> _revokeDevice(Json device) async {
    if (!await _confirm('解绑 ${device['name']}？', '这台电脑需要重新配对才能连接。', '解绑')) {
      return;
    }
    await _update(
      change: () async {
        await _post(
          '/devices/${Uri.encodeComponent('${device['id']}')}/revoke',
        );
        if (_active) unawaited(widget.state.refreshDevice());
      },
    );
  }

  bool _isEnabled(Json account) =>
      account['enabled'] == 1 || account['enabled'] == true;

  Future<void> _toggleAccount(Json account) async {
    final enabled = _isEnabled(account);
    if (enabled &&
        !await _confirm(
          '停用 ${account['username']}？',
          '此账号的登录和设备连接会中断。',
          '停用',
        )) {
      return;
    }
    await _update(
      change: () async {
        await _post(
          '/accounts/${Uri.encodeComponent('${account['id']}')}/enabled',
          {'enabled': !enabled},
        );
      },
    );
  }

  Future<void> _copyCode(Json issued) async {
    if (!_active || !DateTime.now().isBefore(_deadline(issued))) return;
    try {
      await Clipboard.setData(ClipboardData(text: issued['code'] as String));
      if (mounted && widget.state.authenticated) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text(widget.admin ? '已复制邀请码' : '已复制配对码')),
        );
      }
    } on PlatformException {
      if (_active) setState(() => _error = '复制失败，请长按验证码复制。');
    }
  }

  DateTime _deadline(Json issued) => DateTime.fromMillisecondsSinceEpoch(
    ((issued['expiresAt'] as num? ?? 0) * 1000).round(),
  );

  String _expiryLabel(Json issued) {
    final expiry = _deadline(issued);
    final locale = MaterialLocalizations.of(context);
    final time = locale.formatTimeOfDay(
      TimeOfDay.fromDateTime(expiry),
      alwaysUse24HourFormat: MediaQuery.alwaysUse24HourFormatOf(context),
    );
    return '${locale.formatCompactDate(expiry)} $time 到期';
  }

  Widget _heading(String label) => Text(
    label,
    style: Theme.of(
      context,
    ).textTheme.titleMedium?.copyWith(fontWeight: FontWeight.w600),
  );

  Widget _card(List<Widget> children) => Card(
    margin: EdgeInsets.zero,
    color: Theme.of(context).colorScheme.surface,
    elevation: 0,
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(18),
      side: BorderSide(color: Theme.of(context).colorScheme.outlineVariant),
    ),
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: children,
      ),
    ),
  );

  Widget _codeCard() {
    final issued = _issued;
    final valid = issued != null && DateTime.now().isBefore(_deadline(issued));
    return _card([
      _heading(widget.admin ? '邀请新账号' : '配对电脑'),
      const SizedBox(height: 10),
      if (widget.admin)
        TextField(
          controller: _inviteName,
          autocorrect: false,
          enableSuggestions: false,
          textInputAction: TextInputAction.done,
          onSubmitted: (_) => _generateCode(),
          decoration: const InputDecoration(labelText: '新账号名称'),
        )
      else ...[
        const Text('在电脑端 AIRemote 填写服务器地址和配对码。'),
        const SizedBox(height: 4),
        SelectableText(widget.state.api.server),
      ],
      const SizedBox(height: 10),
      FilledButton.icon(
        onPressed: _busy ? null : _generateCode,
        icon: Icon(
          widget.admin ? Icons.person_add_outlined : Icons.link,
          size: 18,
        ),
        label: Text(widget.admin ? '生成邀请码' : '生成配对码'),
      ),
      if (issued != null) ...[
        const Divider(height: 25),
        _heading(widget.admin ? '${issued['username']} 的邀请码' : '一次性配对码'),
        const SizedBox(height: 5),
        if (valid) ...[
          SelectableText(issued['code'] as String),
          Text(_expiryLabel(issued)),
          TextButton.icon(
            onPressed: () => _copyCode(issued),
            icon: const Icon(Icons.copy, size: 16),
            label: const Text('复制'),
          ),
        ] else
          const Text('验证码已过期，请重新生成。'),
      ],
    ]);
  }

  List<Widget> _devices(JsonList devices) => [
    if (devices.isEmpty) const Text('还没有配对的电脑。'),
    for (final device in devices)
      ListTile(
        contentPadding: EdgeInsets.zero,
        leading: Icon(
          device['platform'] == 'windows'
              ? Icons.desktop_windows_outlined
              : Icons.laptop_outlined,
        ),
        title: Text(
          '${device['name']}',
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
        ),
        subtitle: Text(
          '${switch (device['platform']) {
            'windows' => 'Windows',
            'macos' => 'Mac',
            _ => '电脑',
          }} · '
          '${device['revoked'] == true
              ? '已解绑'
              : device['connectorOnline'] == true
              ? '已连接'
              : device['online'] == true
              ? '连接器未就绪'
              : '离线'}',
        ),
        trailing: device['revoked'] == true
            ? null
            : TextButton(
                onPressed: _busy ? null : () => _revokeDevice(device),
                child: const Text('解绑'),
              ),
      ),
  ];

  List<Widget> _administration(Json data, JsonList devices) {
    final invitations = objects(data['invitations']);
    return [
      if (invitations.isNotEmpty) ...[
        _card([
          _heading('待使用的邀请码'),
          for (final invite in invitations)
            ListTile(
              contentPadding: EdgeInsets.zero,
              title: Text('${invite['username']}'),
              subtitle: Text(_expiryLabel(invite)),
              trailing: TextButton(
                onPressed: _busy
                    ? null
                    : () => _update(
                        change: () async {
                          await _post(
                            '/invitations/${Uri.encodeComponent('${invite['id']}')}/revoke',
                          );
                        },
                      ),
                child: const Text('撤销'),
              ),
            ),
        ]),
        const SizedBox(height: 14),
      ],
      _heading('账号和设备'),
      for (final account in objects(data['accounts'])) ...[
        const SizedBox(height: 10),
        _card([
          ListTile(
            contentPadding: EdgeInsets.zero,
            title: Text('${account['username']}'),
            subtitle: Text(
              account['id'] == 'owner'
                  ? '管理员'
                  : _isEnabled(account)
                  ? '已启用'
                  : '已停用',
            ),
            trailing: account['id'] == 'owner'
                ? null
                : TextButton(
                    onPressed: _busy ? null : () => _toggleAccount(account),
                    child: Text(_isEnabled(account) ? '停用账号' : '启用账号'),
                  ),
          ),
          ..._devices(
            devices
                .where((device) => device['account'] == account['id'])
                .toList(),
          ),
        ]),
      ],
    ];
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: widget.state,
    builder: (context, _) {
      final data = _data;
      final devices = objects(data?['devices']);
      return Scaffold(
        appBar: AppBar(
          title: Text(_title),
          actions: [
            if (_active)
              IconButton(
                tooltip: '刷新',
                onPressed: _busy ? null : _update,
                icon: const Icon(Icons.refresh),
              ),
          ],
        ),
        body: SafeArea(
          top: false,
          child: !_active
              ? Center(
                  child: FilledButton(
                    onPressed: () => Navigator.of(
                      context,
                    ).popUntil((route) => route.isFirst),
                    child: const Text('登录已过期，返回登录'),
                  ),
                )
              : Align(
                  alignment: Alignment.topCenter,
                  child: ConstrainedBox(
                    constraints: const BoxConstraints(maxWidth: 600),
                    child: RefreshIndicator(
                      onRefresh: _update,
                      child: ListView(
                        padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
                        physics: const AlwaysScrollableScrollPhysics(),
                        children: [
                          if (_error.isNotEmpty)
                            Padding(
                              padding: const EdgeInsets.only(bottom: 12),
                              child: Text(
                                _error,
                                style: TextStyle(
                                  color: Theme.of(context).colorScheme.error,
                                ),
                              ),
                            ),
                          if (data == null && _busy)
                            const Center(child: CircularProgressIndicator())
                          else if (data != null) ...[
                            if (!widget.admin) ...[
                              ListTile(
                                contentPadding: const EdgeInsets.symmetric(
                                  horizontal: 4,
                                ),
                                leading: const Icon(
                                  Icons.account_circle_outlined,
                                ),
                                title: Text('${data['username'] ?? ''}'),
                                trailing: data['isAdmin'] == true
                                    ? TextButton(
                                        onPressed: () =>
                                            Navigator.of(context).push<void>(
                                              MaterialPageRoute(
                                                builder: (_) => AccountScreen(
                                                  state: widget.state,
                                                  admin: true,
                                                ),
                                              ),
                                            ),
                                        child: const Text('管理员'),
                                      )
                                    : null,
                              ),
                              const SizedBox(height: 10),
                            ],
                            _codeCard(),
                            const SizedBox(height: 14),
                            if (widget.admin)
                              ..._administration(data, devices)
                            else
                              _card([
                                _heading('我的电脑'),
                                const SizedBox(height: 8),
                                ..._devices(devices),
                              ]),
                          ],
                        ],
                      ),
                    ),
                  ),
                ),
        ),
      );
    },
  );
}
