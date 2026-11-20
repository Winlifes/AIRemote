// ignore: deprecated_member_use
import 'package:flutter/material.dart';
import '../core/workspace.dart';

void showClaudeSettings(BuildContext context, Workspace state) {
  final saved = state.chat?['claudeSettings'] as Map?;
  if (saved?['writable'] != true) return;
  final options = <String, String>{
    'model': '${saved?['model'] ?? ''}',
    'effort': '${saved?['effort'] ?? ''}',
    'mode': '${saved?['mode'] ?? 'manual'}',
  };
  var saving = false;
  showModalBottomSheet<void>(
    context: context,
    showDragHandle: true,
    isScrollControlled: true,
    useSafeArea: true,
    builder: (context) => StatefulBuilder(
      builder: (context, update) {
        Widget field(String key, String label, Map<String, String> choices) =>
            DropdownButtonFormField<String>(
              initialValue: options[key],
              isExpanded: true,
              decoration: InputDecoration(labelText: label),
              items: choices.entries
                  .map(
                    (item) => DropdownMenuItem(
                      value: item.key,
                      child: Text(item.value),
                    ),
                  )
                  .toList(),
              onChanged: saving ? null : (value) => options[key] = value!,
            );
        return SafeArea(
          top: false,
          child: SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(22, 0, 22, 20),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  'Claude 工作设置',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const Text('从下一条指令生效。'),
                const SizedBox(height: 16),
                field('model', '模型', {
                  '': '会话默认',
                  'haiku': 'Haiku',
                  'sonnet': 'Sonnet',
                  'opus': 'Opus',
                  'fable': 'Fable',
                }),
                const SizedBox(height: 12),
                field('effort', '思考强度', {
                  '': '模型默认',
                  'low': '低',
                  'medium': '中',
                  'high': '高',
                  'xhigh': '很高',
                  'max': '最高',
                }),
                const SizedBox(height: 12),
                field('mode', '工作模式', {
                  'manual': '工作（使用本机授权规则）',
                  'plan': '计划（只读）',
                }),
                if (state.error.isNotEmpty)
                  Text(
                    state.error,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                const SizedBox(height: 12),
                Align(
                  alignment: Alignment.centerRight,
                  child: FilledButton(
                    onPressed: saving
                        ? null
                        : () async {
                            update(() => saving = true);
                            final success = await state.saveClaudeSettings(
                              options,
                            );
                            if (!context.mounted) return;
                            if (success) {
                              Navigator.pop(context);
                            } else {
                              update(() => saving = false);
                            }
                          },
                    child: Text(saving ? '保存中…' : '保存'),
                  ),
                ),
              ],
            ),
          ),
        );
      },
    ),
  );
}
