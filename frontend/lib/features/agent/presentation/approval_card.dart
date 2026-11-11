import 'package:flutter/material.dart';

import '../domain/agent_view_data.dart';
import 'confirmation_card.dart';

typedef ApprovalDecisionCallback = void Function({
  required bool approved,
  String? selectedOptionId,
  List<String>? approvedOperationIds,
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

/// Builds the deterministic replacement request for a purchase quantity edit.
String? buildPurchaseQuantityEditMessage({
  required String name,
  required num originalQuantity,
  required num editedQuantity,
}) => originalQuantity == editedQuantity
    ? null
    : '$name：數量改成 ${_quantityText(editedQuantity)}';

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
    ApprovalPresentation.purchaseOptions
        when approval.operations.every(
          (operation) => operation.action.isEmpty,
        ) =>
      _GenericApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
      ),
    ApprovalPresentation.purchaseOptions => _PurchaseOptionsApprovalCard(
      approval: approval,
      busy: busy,
      onDecision: onDecision,
    ),
    ApprovalPresentation.singleAction || ApprovalPresentation.batchAction
        when approval.operations.every(
          (operation) => operation.action == 'complete_purchase',
        ) =>
      _PurchaseApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
        onSubmitChanges: onSubmitChanges,
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

class _PurchaseValues {
  const _PurchaseValues({
    required this.operation,
    required this.name,
    required this.quantity,
    required this.unit,
  });

  final OperationView operation;
  final String name;
  final num quantity;
  final String unit;
}

_PurchaseValues? _purchaseValues(OperationView operation) {
  final name = operation.values['name'];
  final quantity = operation.values['purchased_quantity'];
  final unit = operation.values['purchased_unit'];
  if (name is! String ||
      name.trim().isEmpty ||
      quantity is! num ||
      unit is! String ||
      unit.trim().isEmpty) {
    return null;
  }
  return _PurchaseValues(
    operation: operation,
    name: name.trim(),
    quantity: quantity,
    unit: unit.trim(),
  );
}

class _PurchaseApprovalCard extends StatefulWidget {
  const _PurchaseApprovalCard({
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
  State<_PurchaseApprovalCard> createState() => _PurchaseApprovalCardState();
}

class _PurchaseApprovalCardState extends State<_PurchaseApprovalCard> {
  late final List<_PurchaseValues> _items;
  late final Set<String> _selectedOperationIds;
  late final TextEditingController _quantityController;
  bool _editingQuantity = false;

  bool get _isBatch =>
      widget.approval.presentation == ApprovalPresentation.batchAction;

  @override
  void initState() {
    super.initState();
    _items = widget.approval.operations
        .map(_purchaseValues)
        .whereType<_PurchaseValues>()
        .toList(growable: false);
    _selectedOperationIds = _items.map((item) => item.operation.id).toSet();
    _quantityController = TextEditingController(
      text: _items.length == 1 ? _quantityText(_items.single.quantity) : '',
    );
  }

  @override
  void dispose() {
    _quantityController.dispose();
    super.dispose();
  }

  num? get _editedQuantity => num.tryParse(_quantityController.text.trim());

  @override
  Widget build(BuildContext context) {
    if (_items.length != widget.approval.operations.length) {
      return _GenericApprovalCard(
        approval: widget.approval,
        busy: widget.busy,
        onDecision: widget.onDecision,
      );
    }
    final singleItem = _items.length == 1 ? _items.single : null;
    final editedQuantity = _editedQuantity;
    final editMessage = singleItem == null || editedQuantity == null
        ? null
        : buildPurchaseQuantityEditMessage(
            name: singleItem.name,
            originalQuantity: singleItem.quantity,
            editedQuantity: editedQuantity,
          );
    return Card(
      key: const Key('purchase-approval-card'),
      color: Theme.of(context).colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              _isBatch ? '確認完成購買項目' : '確認完成購買',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            const SizedBox(height: 8),
            for (final item in _items)
              _isBatch
                  ? CheckboxListTile(
                      key: Key('purchase-operation-${item.operation.id}'),
                      contentPadding: EdgeInsets.zero,
                      value: _selectedOperationIds.contains(item.operation.id),
                      onChanged: widget.busy
                          ? null
                          : (selected) => setState(() {
                              if (selected ?? false) {
                                _selectedOperationIds.add(item.operation.id);
                              } else {
                                _selectedOperationIds.remove(item.operation.id);
                              }
                            }),
                      title: _PurchaseLine(item: item),
                    )
                  : Row(
                      children: [
                        Expanded(child: _PurchaseLine(item: item)),
                        if (!_editingQuantity && !widget.busy)
                          IconButton(
                            key: const Key('purchase-edit-quantity'),
                            tooltip: '修改數量',
                            onPressed: () =>
                                setState(() => _editingQuantity = true),
                            icon: const Icon(Icons.edit_outlined),
                          ),
                      ],
                    ),
            if (!_isBatch && _editingQuantity) ...[
              const SizedBox(height: 8),
              TextField(
                key: const Key('purchase-quantity-field'),
                controller: _quantityController,
                enabled: !widget.busy,
                keyboardType: const TextInputType.numberWithOptions(
                  decimal: true,
                ),
                decoration: const InputDecoration(labelText: '購入數量'),
                onChanged: (_) => setState(() {}),
              ),
              if (editMessage != null && !widget.busy)
                Align(
                  alignment: Alignment.centerRight,
                  child: TextButton(
                    key: const Key('purchase-submit-quantity-change'),
                    onPressed: widget.onSubmitChanges == null
                        ? null
                        : () => widget.onSubmitChanges!(editMessage),
                    child: const Text('送出修改'),
                  ),
                ),
            ],
            const SizedBox(height: 8),
            Wrap(
              spacing: 12,
              children: [
                TextButton(
                  key: const Key('approval-cancel'),
                  onPressed: widget.busy
                      ? null
                      : () => widget.onDecision(approved: false),
                  child: const Text('取消'),
                ),
                FilledButton(
                  key: const Key('approval-confirm'),
                  onPressed:
                      widget.busy || (_isBatch && _selectedOperationIds.isEmpty)
                      ? null
                      : () => widget.onDecision(
                          approved: true,
                          approvedOperationIds: _isBatch
                              ? _items
                                    .where(
                                      (item) => _selectedOperationIds.contains(
                                        item.operation.id,
                                      ),
                                    )
                                    .map((item) => item.operation.id)
                                    .toList(growable: false)
                              : null,
                        ),
                  child: const Text('確認'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}

class _PurchaseLine extends StatelessWidget {
  const _PurchaseLine({required this.item});

  final _PurchaseValues item;

  @override
  Widget build(BuildContext context) =>
      Text('${item.name} ${_quantityText(item.quantity)} ${item.unit}');
}

class _PurchaseOptionsApprovalCard extends StatefulWidget {
  const _PurchaseOptionsApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;

  @override
  State<_PurchaseOptionsApprovalCard> createState() =>
      _PurchaseOptionsApprovalCardState();
}

class _PurchaseOptionsApprovalCardState
    extends State<_PurchaseOptionsApprovalCard> {
  String? _selectedOptionId;

  @override
  Widget build(BuildContext context) => Card(
    key: const Key('purchase-options-approval-card'),
    color: Theme.of(context).colorScheme.secondaryContainer,
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('選擇購買選項', style: Theme.of(context).textTheme.titleMedium),
          RadioGroup<String>(
            groupValue: _selectedOptionId,
            onChanged: widget.busy
                ? (_) {}
                : (id) => setState(() => _selectedOptionId = id),
            child: Column(
              children: [
                for (final option in widget.approval.options)
                  RadioListTile<String>(
                    key: Key('purchase-option-${option.id}'),
                    contentPadding: EdgeInsets.zero,
                    value: option.id,
                    enabled: !widget.busy,
                    title: _PurchaseOptionDetails(option: option),
                  ),
              ],
            ),
          ),
          Wrap(
            spacing: 12,
            children: [
              TextButton(
                key: const Key('approval-cancel'),
                onPressed: widget.busy
                    ? null
                    : () => widget.onDecision(approved: false),
                child: const Text('取消'),
              ),
              FilledButton(
                key: const Key('approval-confirm'),
                onPressed: widget.busy || _selectedOptionId == null
                    ? null
                    : () => widget.onDecision(
                        approved: true,
                        selectedOptionId: _selectedOptionId,
                      ),
                child: const Text('確認'),
              ),
            ],
          ),
        ],
      ),
    ),
  );
}

class _PurchaseOptionDetails extends StatelessWidget {
  const _PurchaseOptionDetails({required this.option});

  final ApprovalOptionView option;

  @override
  Widget build(BuildContext context) {
    final entries = option.values.entries.where(
      (entry) =>
          entry.key != 'option_id' &&
          entry.key != 'label' &&
          !entry.key.endsWith('_id') &&
          (entry.value is String || entry.value is num),
    );
    final details = entries
        .map((entry) => '${_optionFieldLabel(entry.key)}：${entry.value}')
        .toList(growable: false);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (option.label != null) Text(option.label!),
        if (details.isNotEmpty) Text(details.join('　')),
      ],
    );
  }
}

String _optionFieldLabel(String key) => switch (key) {
  'store' => '商店',
  'price' || 'estimated_price' => '價格',
  'source' => '來源',
  _ => key,
};

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
                  if (option.label != null)
                    FilledButton(
                      key: Key('approval-option-${option.id}'),
                      onPressed: busy
                          ? null
                          : () => onDecision(
                              approved: true,
                              selectedOptionId: option.id,
                            ),
                      child: Text(option.label!),
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
