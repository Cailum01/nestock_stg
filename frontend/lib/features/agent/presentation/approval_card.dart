import 'package:flutter/material.dart';

import '../domain/agent_view_data.dart';
import 'confirmation_card.dart';

typedef ApprovalDecisionCallback = void Function({
  required bool approved,
  String? selectedOptionId,
});

/// Builds the deterministic text used only when a user submits an edited card.
/// The Agent, not Flutter, reinterprets the text and issues any replacement card.
String? buildApprovalEditMessage({
  required ConfirmationItemView original,
  required ConfirmationItemView edited,
}) {
  final changes = <String>[];
  if (original.name != edited.name) changes.add('品名改成 ${edited.name}');
  if (original.quantity != edited.quantity) {
    changes.add('數量改成 ${_quantityText(edited.quantity)}');
  }
  if (original.unit != edited.unit) changes.add('單位改成 ${edited.unit}');
  if (original.location != edited.location) {
    changes.add('位置改成 ${edited.location}');
  }
  if (changes.isEmpty) return null;
  return '${original.name}：${changes.join('，')}';
}

String _quantityText(num quantity) => quantity == quantity.truncateToDouble()
    ? quantity.toStringAsFixed(0)
    : quantity.toString();

class ApprovalCard extends StatelessWidget {
  const ApprovalCard({
    super.key,
    required this.approval,
    required this.busy,
    required this.onDecision,
    this.onSubmitChanges,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<String>? onSubmitChanges;

  @override
  Widget build(BuildContext context) => switch (approval.presentation) {
    ApprovalPresentation.itemMatch
        when approval.operations.first.action == 'confirm_item_match' =>
      _ItemMatchApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
      ),
    ApprovalPresentation.singleAction
        when approval.operations.first.action.isNotEmpty =>
      _SingleActionApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
        onSubmitChanges: onSubmitChanges,
      ),
    _ => _GenericApprovalCard(
      approval: approval,
      busy: busy,
      onDecision: onDecision,
    ),
  };
}

class _SingleActionApprovalCard extends StatelessWidget {
  const _SingleActionApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
    required this.onSubmitChanges,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<String>? onSubmitChanges;

  @override
  Widget build(BuildContext context) {
    final operation = approval.operations.first;
    final item = _confirmationItem(operation);
    return ConfirmationCard(
      title: operation.action == 'create_item' ? '確認建立庫存品項' : '確認操作',
      description: operation.description,
      items: item == null ? const [] : [item],
      details: item == null ? operation.values : const {},
      allowExclusion: false,
      onCancel: busy ? null : () => onDecision(approved: false),
      onConfirm: busy ? null : () => onDecision(approved: true),
      onSubmitChanges: item == null || busy || onSubmitChanges == null
          ? null
          : (drafts) {
              final message = buildApprovalEditMessage(
                original: item,
                edited: drafts.single,
              );
              if (message != null) onSubmitChanges!(message);
            },
    );
  }
}

ConfirmationItemView? _confirmationItem(OperationView operation) {
  final values = operation.values;
  final name = values['name'];
  final quantity = values['quantity'];
  final unit = values['unit'];
  final location = values['location'];
  if (name is! String ||
      name.trim().isEmpty ||
      quantity is! num ||
      unit is! String ||
      unit.trim().isEmpty ||
      location is! String ||
      location.trim().isEmpty) {
    return null;
  }
  return ConfirmationItemView(
    name: name.trim(),
    quantity: quantity,
    unit: unit.trim(),
    location: location.trim(),
  );
}

class _ItemMatchApprovalCard extends StatelessWidget {
  const _ItemMatchApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;

  @override
  Widget build(BuildContext context) {
    final operation = approval.operations.first;
    final values = operation.values;
    final userTerm = values['user_term'];
    final candidateName = values['candidate_name'];
    final quantity = values['quantity'];
    final unit = values['unit'];
    return Card(
      key: const Key('item-match-approval-card'),
      color: Theme.of(context).colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('確認品項比對', style: Theme.of(context).textTheme.titleMedium),
            const SizedBox(height: 8),
            Text(operation.description),
            if (userTerm is String && userTerm.trim().isNotEmpty)
              Text('你說的是：${userTerm.trim()}'),
            if (candidateName is String && candidateName.trim().isNotEmpty)
              Text('家中品項：${candidateName.trim()}'),
            if (quantity is num || unit is String)
              Text(
                '數量：${quantity ?? ''}${unit is String ? ' ${unit.trim()}' : ''}',
              ),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final option in approval.options)
                  FilledButton(
                    key: Key('approval-option-${option.id}'),
                    onPressed: busy
                        ? null
                        : () => onDecision(
                            approved: true,
                            selectedOptionId: option.id,
                          ),
                    child: Text(option.label),
                  ),
                TextButton(
                  key: const Key('approval-cancel'),
                  onPressed: busy ? null : () => onDecision(approved: false),
                  child: const Text('取消'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _GenericApprovalCard extends StatelessWidget {
  const _GenericApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;

  @override
  Widget build(BuildContext context) {
    final isFixture = approval.operations.any(
      (operation) => operation.action.isEmpty,
    );
    final title = isFixture
        ? switch (approval.presentation) {
            ApprovalPresentation.singleAction => '單一操作確認示範',
            ApprovalPresentation.batchAction => '批次操作確認示範',
            ApprovalPresentation.purchaseOptions => '選購項目確認示範',
            ApprovalPresentation.itemMatch => '品項比對確認示範',
          }
        : '確認操作';
    return Card(
      color: Theme.of(context).colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(title, style: Theme.of(context).textTheme.titleMedium),
            for (final operation in approval.operations)
              Padding(
                padding: const EdgeInsets.symmetric(vertical: 8),
                child: Text(operation.description),
              ),
            if (isFixture) const Text('此示範不會寫入資料，也不會下單或付款。'),
            Wrap(
              spacing: 12,
              children: [
                TextButton(
                  key: const Key('approval-cancel'),
                  onPressed: busy ? null : () => onDecision(approved: false),
                  child: const Text('取消'),
                ),
                FilledButton(
                  key: const Key('approval-confirm'),
                  onPressed: busy ? null : () => onDecision(approved: true),
                  child: Text(isFixture ? '確認示範' : '確認'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
