import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import 'approval_card_config.dart';
import 'confirmation_card.dart';
import 'hand_drawn_card_shell.dart';

typedef ApprovalDecisionCallback = void Function({
  required bool approved,
  String? selectedOptionId,
  List<String>? approvedOperationIds,
});

Future<void> _openExternalProductUrl(Uri url) async {
  await launchUrl(url, mode: LaunchMode.externalApplication);
}

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
  if (original.location != edited.location && edited.location != null) {
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
    this.onSubmitCardChanges,
    this.changeErrors = const {},
    this.config = const ApprovalCardConfig(),
    this.onOpenProductUrl,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<String>? onSubmitChanges;
  final ValueChanged<List<CardChange>>? onSubmitCardChanges;

  /// Backend per-operation card-change refusals, if it supplied any.
  final Map<String, String> changeErrors;
  final ApprovalCardConfig config;
  final Future<void> Function(Uri url)? onOpenProductUrl;

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
      onOpenProductUrl: onOpenProductUrl ?? _openExternalProductUrl,
    ),
    ApprovalPresentation.singleAction || ApprovalPresentation.batchAction
        when approval.operations.every(_isPurchaseWorkflowOperation) =>
      _PurchaseApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
        onSubmitCardChanges: onSubmitCardChanges,
        changeErrors: changeErrors,
        datedPurchasesEnabled: config.datedPurchasesEnabled,
      ),
    ApprovalPresentation.batchAction
        when approval.operations.every(_isPhotoCreateItemOperation) =>
      _CreateItemBatchApprovalCard(
        approval: approval,
        busy: busy,
        onDecision: onDecision,
        onSubmitCardChanges: onSubmitCardChanges,
        changeErrors: changeErrors,
      ),
    // A batch which mixes a photo-created item with purchase workflow lines has
    // no dedicated contract presentation. Keep it generic rather than treating
    // a photo item as a Shopping-backed purchase.
    ApprovalPresentation.batchAction => _GenericBatchApprovalCard(
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
        onSubmitCardChanges: onSubmitCardChanges,
        changeErrors: changeErrors,
      ),
    _ => _GenericApprovalCard(
      approval: approval,
      busy: busy,
      onDecision: onDecision,
    ),
  };
}

bool _isPurchaseWorkflowOperation(OperationView operation) =>
    operation.action == 'complete_purchase' ||
    (operation.action == 'create_item' &&
        _hasNonBlankShoppingItemId(operation.values['shopping_item_id']));

bool _hasNonBlankShoppingItemId(Object? value) =>
    value is String && value.trim().isNotEmpty;

bool _isPhotoCreateItemOperation(OperationView operation) =>
    operation.action == 'create_item' &&
    !_isPurchaseWorkflowOperation(operation);

/// Photo and said-new-item cards share this contract. Shopping-backed
/// `create_item` operations are purchase workflow lines and remain read-only
/// except for their dedicated purchase quantity editor.
bool _isEditableNewItemOperation(OperationView operation) =>
    _isPhotoCreateItemOperation(operation);

class _PurchaseValues {
  const _PurchaseValues({
    required this.operation,
    required this.name,
    required this.quantity,
    required this.unit,
    required this.purchasedOn,
  });

  final OperationView operation;
  final String name;
  final num quantity;
  final String unit;

  /// A Taipei calendar day in the backend's YYYY-MM-DD wire format.
  final String? purchasedOn;

  String? get purchaseDateLabel => purchasedOn?.replaceAll('-', '/');
}

_PurchaseValues? _purchaseValues(OperationView operation) {
  final name = operation.values['name'];
  final isShoppingBackedCreateItem =
      operation.action == 'create_item' &&
      _isPurchaseWorkflowOperation(operation);
  final quantity = operation
      .values[isShoppingBackedCreateItem ? 'quantity' : 'purchased_quantity'];
  final unit =
      operation.values[isShoppingBackedCreateItem ? 'unit' : 'purchased_unit'];
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
    purchasedOn: _taipeiPurchaseDay(operation.values['purchased_at']),
  );
}

/// Converts the persisted instant to the purchase calendar day in Taipei.
String? _taipeiPurchaseDay(Object? rawValue) {
  if (rawValue is! String) return null;
  final text = rawValue.trim();
  final source = RegExp(r'^(\d{4})-(\d{2})-(\d{2})T').firstMatch(text);
  if (source == null) return null;
  final sourceDay = DateTime(
    int.parse(source.group(1)!),
    int.parse(source.group(2)!),
    int.parse(source.group(3)!),
  );
  if (sourceDay.year != int.parse(source.group(1)!) ||
      sourceDay.month != int.parse(source.group(2)!) ||
      sourceDay.day != int.parse(source.group(3)!)) {
    return null;
  }
  final instant = DateTime.tryParse(text);
  if (instant == null) return null;
  final taipei = instant.toUtc().add(const Duration(hours: 8));
  return _formatPurchaseDay(taipei);
}

final _purchaseDayPattern = RegExp(r'^\d{4}-\d{2}-\d{2}$');

String _formatPurchaseDay(DateTime day) =>
    '${day.year.toString().padLeft(4, '0')}-${day.month.toString().padLeft(2, '0')}-${day.day.toString().padLeft(2, '0')}';

DateTime _taipeiToday() {
  final now = DateTime.now().toUtc().add(const Duration(hours: 8));
  return DateTime(now.year, now.month, now.day);
}

bool _isValidPurchaseDay(String value) {
  if (!_purchaseDayPattern.hasMatch(value)) return false;
  final parts = value.split('-').map(int.parse).toList(growable: false);
  final parsed = DateTime(parts[0], parts[1], parts[2]);
  return parsed.year == parts[0] &&
      parsed.month == parts[1] &&
      parsed.day == parts[2];
}

class _PurchaseApprovalCard extends StatefulWidget {
  const _PurchaseApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
    required this.onSubmitCardChanges,
    required this.changeErrors,
    required this.datedPurchasesEnabled,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<List<CardChange>>? onSubmitCardChanges;
  final Map<String, String> changeErrors;
  final bool datedPurchasesEnabled;

  @override
  State<_PurchaseApprovalCard> createState() => _PurchaseApprovalCardState();
}

class _PurchaseApprovalCardState extends State<_PurchaseApprovalCard> {
  late final List<_PurchaseValues> _items;
  late final Set<String> _selectedOperationIds;
  late final Map<String, TextEditingController> _quantityControllers;
  final Set<String> _editingOperationIds = {};
  final Map<String, String?> _purchasedOnDrafts = {};

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
    _quantityControllers = {
      for (final item in _items)
        item.operation.id: TextEditingController(
          text: _quantityText(item.quantity),
        ),
    };
  }

  @override
  void dispose() {
    for (final controller in _quantityControllers.values) {
      controller.dispose();
    }
    super.dispose();
  }

  int? _editedQuantity(_PurchaseValues item) {
    final quantity = int.tryParse(
      _quantityControllers[item.operation.id]!.text.trim(),
    );
    return quantity != null && quantity >= 1 ? quantity : null;
  }

  Key _quantityFieldKey(_PurchaseValues item) => Key(
    _isBatch
        ? 'purchase-quantity-field-${item.operation.id}'
        : 'purchase-quantity-field',
  );

  Key _quantityEditKey(_PurchaseValues item) => Key(
    _isBatch
        ? 'purchase-edit-quantity-${item.operation.id}'
        : 'purchase-edit-quantity',
  );

  String? _effectivePurchasedOn(_PurchaseValues item) =>
      _purchasedOnDrafts.containsKey(item.operation.id)
      ? _purchasedOnDrafts[item.operation.id]
      : item.purchasedOn;

  List<CardChange> get _changes {
    final changes = <CardChange>[];
    for (final item in _items) {
      final quantity = _editedQuantity(item);
      if (quantity == null) continue;
      final quantityChanged = quantity != item.quantity;
      final hasDateDraft = _purchasedOnDrafts.containsKey(item.operation.id);
      final dateDraft = _purchasedOnDrafts[item.operation.id];
      final dateChanged = hasDateDraft && dateDraft != item.purchasedOn;
      if (!quantityChanged && !dateChanged) continue;
      changes.add(
        CardChange(
          operationId: item.operation.id,
          quantity: quantityChanged ? quantity : null,
          purchasedOn: dateChanged ? dateDraft : null,
          resetPurchasedOn: dateChanged && dateDraft == null,
        ),
      );
    }
    return changes;
  }

  bool get _hasInvalidEditedQuantity => _editingOperationIds.any((id) {
    final value = _quantityControllers[id]!.text.trim();
    final quantity = int.tryParse(value);
    return quantity == null || quantity < 1;
  });

  bool get _hasUnsentDraft => _changes.isNotEmpty || _hasInvalidEditedQuantity;

  Future<void> _pickPurchaseDay(_PurchaseValues item) async {
    final initialText = _effectivePurchasedOn(item);
    final initialDate = initialText != null && _isValidPurchaseDay(initialText)
        ? DateTime.parse(initialText)
        : _taipeiToday();
    final selected = await showDatePicker(
      context: context,
      initialDate: initialDate.isAfter(_taipeiToday())
          ? _taipeiToday()
          : initialDate,
      firstDate: DateTime(2000),
      lastDate: _taipeiToday(),
      helpText: '選擇購買日期',
    );
    if (selected == null || !mounted) return;
    final selectedDay = _formatPurchaseDay(selected);
    if (selected.isAfter(_taipeiToday())) return;
    setState(() => _purchasedOnDrafts[item.operation.id] = selectedDay);
  }

  Widget _quantityEditor(_PurchaseValues item) {
    final date = _effectivePurchasedOn(item);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const SizedBox(height: 8),
        TextField(
          key: _quantityFieldKey(item),
          controller: _quantityControllers[item.operation.id],
          enabled: !widget.busy,
          keyboardType: TextInputType.number,
          decoration: const InputDecoration(labelText: '購入數量'),
          onChanged: (_) => setState(() {}),
        ),
        if (widget.datedPurchasesEnabled) ...[
          const SizedBox(height: 4),
          Wrap(
            spacing: 8,
            children: [
              TextButton(
                key: Key('purchase-edit-date-${item.operation.id}'),
                onPressed: widget.busy ? null : () => _pickPurchaseDay(item),
                child: Text(date == null ? '補填購買日期' : '修改購買日期'),
              ),
              if (date != null)
                TextButton(
                  key: Key('purchase-reset-date-${item.operation.id}'),
                  onPressed: widget.busy
                      ? null
                      : () => setState(
                          () => _purchasedOnDrafts[item.operation.id] = null,
                        ),
                  child: const Text('改回今天'),
                ),
            ],
          ),
        ],
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_items.length != widget.approval.operations.length) {
      return _GenericApprovalCard(
        approval: widget.approval,
        busy: widget.busy,
        onDecision: widget.onDecision,
      );
    }
    return HandDrawnCardShell(
      key: const Key('purchase-approval-card'),
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
                    title: Row(
                      children: [
                        Expanded(
                          child: _PurchaseLine(
                            item: item,
                            error: widget.changeErrors[item.operation.id],
                          ),
                        ),
                        if (!_editingOperationIds.contains(item.operation.id) &&
                            !widget.busy)
                          IconButton(
                            key: _quantityEditKey(item),
                            tooltip: '修改數量',
                            onPressed: () => setState(
                              () => _editingOperationIds.add(item.operation.id),
                            ),
                            icon: const Icon(Icons.edit_outlined),
                          ),
                      ],
                    ),
                    subtitle: _editingOperationIds.contains(item.operation.id)
                        ? _quantityEditor(item)
                        : null,
                  )
                : Row(
                    children: [
                      Expanded(
                        child: _PurchaseLine(
                          item: item,
                          error: widget.changeErrors[item.operation.id],
                        ),
                      ),
                      if (!_editingOperationIds.contains(item.operation.id) &&
                          !widget.busy)
                        IconButton(
                          key: _quantityEditKey(item),
                          tooltip: '修改數量',
                          onPressed: () => setState(
                            () => _editingOperationIds.add(item.operation.id),
                          ),
                          icon: const Icon(Icons.edit_outlined),
                        ),
                    ],
                  ),
          if (!_isBatch && _editingOperationIds.isNotEmpty)
            _quantityEditor(_items.single),
          if (_changes.isNotEmpty && !_hasInvalidEditedQuantity && !widget.busy)
            Align(
              alignment: Alignment.centerRight,
              child: OutlinedButton(
                key: const Key('purchase-submit-card-changes'),
                onPressed: widget.onSubmitCardChanges == null
                    ? null
                    : () => widget.onSubmitCardChanges!(_changes),
                child: const Text('送出修改'),
              ),
            ),
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
                    widget.busy ||
                        _hasUnsentDraft ||
                        (_isBatch && _selectedOperationIds.isEmpty)
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
    );
  }
}

class _PurchaseLine extends StatelessWidget {
  const _PurchaseLine({required this.item, this.error});

  final _PurchaseValues item;
  final String? error;

  @override
  Widget build(BuildContext context) => Column(
    crossAxisAlignment: CrossAxisAlignment.start,
    children: [
      Text('${item.name} ${_quantityText(item.quantity)} ${item.unit}'),
      if (item.purchaseDateLabel != null)
        Text(
          '購買日期：${item.purchaseDateLabel}',
          key: Key('purchase-date-${item.operation.id}'),
        ),
      if (error != null)
        Text(
          error!,
          key: Key('card-change-error-${item.operation.id}'),
          style: TextStyle(color: Theme.of(context).colorScheme.error),
        ),
    ],
  );
}

class _PurchaseOptionsApprovalCard extends StatefulWidget {
  const _PurchaseOptionsApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
    required this.onOpenProductUrl,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final Future<void> Function(Uri url) onOpenProductUrl;

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
                    title: _PurchaseOptionDetails(
                      option: option,
                      onOpenProductUrl: widget.onOpenProductUrl,
                    ),
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
  const _PurchaseOptionDetails({
    required this.option,
    required this.onOpenProductUrl,
  });

  final ApprovalOptionView option;
  final Future<void> Function(Uri url) onOpenProductUrl;

  Uri? get _productUrl {
    final rawUrl = option.values['product_url'];
    if (rawUrl is! String || rawUrl.trim().isEmpty) return null;
    final url = Uri.tryParse(rawUrl.trim());
    if (url == null ||
        url.host.isEmpty ||
        (url.scheme != 'http' && url.scheme != 'https')) {
      return null;
    }
    return url;
  }

  Future<void> _openProductUrl(Uri url) async {
    try {
      await onOpenProductUrl(url);
    } catch (_) {
      // Launch failures must not affect selection or approval state.
    }
  }

  @override
  Widget build(BuildContext context) {
    final entries = option.values.entries.where(
      (entry) =>
          entry.key != 'option_id' &&
          entry.key != 'label' &&
          entry.key != 'product_url' &&
          !entry.key.endsWith('_id') &&
          (entry.value is String || entry.value is num),
    );
    final details = entries
        .map((entry) => '${_optionFieldLabel(entry.key)}：${entry.value}')
        .toList(growable: false);
    final productUrl = _productUrl;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (option.label != null) Text(option.label!),
        if (details.isNotEmpty) Text(details.join('　')),
        if (productUrl != null)
          TextButton(
            key: Key('purchase-option-link-${option.id}'),
            onPressed: () => _openProductUrl(productUrl),
            child: const Text('前往購買'),
          ),
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
    required this.onSubmitCardChanges,
    required this.changeErrors,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<String>? onSubmitChanges;
  final ValueChanged<List<CardChange>>? onSubmitCardChanges;
  final Map<String, String> changeErrors;

  @override
  Widget build(BuildContext context) {
    final operation = approval.operations.first;
    final item = _confirmationItem(operation);
    return ConfirmationCard(
      title: operation.action == 'create_item' ? '確認建立庫存品項' : '確認操作',
      description: operation.description,
      items: item == null ? const [] : [item],
      details: item == null ? operation.values : const {},
      changeError: changeErrors[operation.id],
      allowExclusion: false,
      onCancel: busy ? null : () => onDecision(approved: false),
      onConfirm: busy ? null : () => onDecision(approved: true),
      editableFields: _isEditableNewItemOperation(operation)
          ? const {
              ConfirmationDraftField.name,
              ConfirmationDraftField.quantity,
              ConfirmationDraftField.location,
            }
          : null,
      onSubmitChanges: item == null || busy
          ? null
          : (drafts) {
              if (_isEditableNewItemOperation(operation)) {
                final edited = drafts.single;
                final quantity = edited.quantity;
                final name = edited.name != item.name ? edited.name : null;
                final quantityChanged =
                    quantity == quantity.truncateToDouble() &&
                    quantity != item.quantity;
                final locationChanged = edited.location != item.location;
                if (name != null || quantityChanged || locationChanged) {
                  onSubmitCardChanges?.call([
                    CardChange(
                      operationId: operation.id,
                      name: name,
                      quantity: quantityChanged ? quantity.toInt() : null,
                      location: locationChanged ? edited.location : null,
                      clearLocation: locationChanged && edited.location == null,
                    ),
                  ]);
                }
                return;
              }
              if (onSubmitChanges == null) return;
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
  final normalizedLocation = location is String && location.trim().isNotEmpty
      ? location.trim()
      : null;
  if (name is! String ||
      name.trim().isEmpty ||
      quantity is! num ||
      unit is! String ||
      unit.trim().isEmpty ||
      (normalizedLocation == null && operation.action != 'create_item')) {
    return null;
  }
  return ConfirmationItemView(
    name: name.trim(),
    quantity: quantity,
    unit: unit.trim(),
    location: normalizedLocation,
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

class _CreateItemBatchApprovalCard extends StatefulWidget {
  const _CreateItemBatchApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
    required this.onSubmitCardChanges,
    required this.changeErrors,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;
  final ValueChanged<List<CardChange>>? onSubmitCardChanges;
  final Map<String, String> changeErrors;

  @override
  State<_CreateItemBatchApprovalCard> createState() =>
      _CreateItemBatchApprovalCardState();
}

class _CreateItemBatchApprovalCardState
    extends State<_CreateItemBatchApprovalCard> {
  late final Set<String> _selectedOperationIds;
  late final Map<String, TextEditingController> _nameControllers;
  late final Map<String, TextEditingController> _quantityControllers;
  late final Map<String, TextEditingController> _locationControllers;
  final Set<String> _editingOperationIds = {};

  @override
  void initState() {
    super.initState();
    _selectedOperationIds = widget.approval.operations
        .map((operation) => operation.id)
        .toSet();
    _nameControllers = {
      for (final operation in widget.approval.operations)
        operation.id: TextEditingController(
          text: operation.values['name'] as String? ?? '',
        ),
    };
    _quantityControllers = {
      for (final operation in widget.approval.operations)
        operation.id: TextEditingController(
          text: _quantityText(operation.values['quantity'] as num),
        ),
    };
    _locationControllers = {
      for (final operation in widget.approval.operations)
        operation.id: TextEditingController(
          text: _locationText(operation.values['location']),
        ),
    };
  }

  @override
  void dispose() {
    for (final controller in [
      ..._nameControllers.values,
      ..._quantityControllers.values,
      ..._locationControllers.values,
    ]) {
      controller.dispose();
    }
    super.dispose();
  }

  List<CardChange> get _changes {
    final changes = <CardChange>[];
    for (final operation in widget.approval.operations) {
      final name = _nameControllers[operation.id]!.text.trim();
      final quantity = int.tryParse(
        _quantityControllers[operation.id]!.text.trim(),
      );
      if (name.isEmpty || quantity == null || quantity < 1) continue;
      final originalName = operation.values['name'] as String? ?? '';
      final originalLocation = _normalizedLocation(
        operation.values['location'],
      );
      final location = _normalizedLocation(
        _locationControllers[operation.id]!.text,
      );
      final nameChanged = name != originalName.trim();
      final quantityChanged = quantity != operation.values['quantity'];
      final locationChanged = location != originalLocation;
      if (nameChanged || quantityChanged || locationChanged) {
        changes.add(
          CardChange(
            operationId: operation.id,
            name: nameChanged ? name : null,
            quantity: quantityChanged ? quantity : null,
            location: locationChanged ? location : null,
            clearLocation: locationChanged && location == null,
          ),
        );
      }
    }
    return changes;
  }

  bool get _hasInvalidEditedDraft => _editingOperationIds.any((id) {
    if (_nameControllers[id]!.text.trim().isEmpty) return true;
    final quantity = int.tryParse(_quantityControllers[id]!.text.trim());
    return quantity == null || quantity < 1;
  });

  bool get _hasUnsentDraft => _changes.isNotEmpty || _hasInvalidEditedDraft;

  @override
  Widget build(BuildContext context) => HandDrawnCardShell(
    key: const Key('create-item-batch-approval-card'),
    child: Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('確認建立庫存品項', style: Theme.of(context).textTheme.titleMedium),
        for (final operation in widget.approval.operations) ...[
          CheckboxListTile(
            key: Key('create-item-operation-${operation.id}'),
            contentPadding: EdgeInsets.zero,
            value: _selectedOperationIds.contains(operation.id),
            onChanged: widget.busy
                ? null
                : (selected) => setState(() {
                    if (selected ?? false) {
                      _selectedOperationIds.add(operation.id);
                    } else {
                      _selectedOperationIds.remove(operation.id);
                    }
                  }),
            title: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(_createItemLabel(operation)),
                Text(_createItemLocationLabel(operation)),
                if (widget.changeErrors[operation.id] case final error?)
                  Text(
                    error,
                    key: Key('card-change-error-${operation.id}'),
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
              ],
            ),
            secondary: IconButton(
              key: Key('create-item-edit-quantity-${operation.id}'),
              tooltip: '修改數量',
              onPressed: widget.busy
                  ? null
                  : () =>
                        setState(() => _editingOperationIds.add(operation.id)),
              icon: const Icon(Icons.edit_outlined),
            ),
            subtitle: _editingOperationIds.contains(operation.id)
                ? Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      TextField(
                        key: Key('create-item-name-${operation.id}'),
                        controller: _nameControllers[operation.id],
                        onChanged: (_) => setState(() {}),
                        decoration: const InputDecoration(labelText: '品名'),
                      ),
                      TextField(
                        key: Key('create-item-quantity-${operation.id}'),
                        controller: _quantityControllers[operation.id],
                        keyboardType: TextInputType.number,
                        onChanged: (_) => setState(() {}),
                        decoration: const InputDecoration(labelText: '數量'),
                      ),
                      TextField(
                        key: Key('create-item-location-${operation.id}'),
                        controller: _locationControllers[operation.id],
                        onChanged: (_) => setState(() {}),
                        decoration: const InputDecoration(labelText: '位置'),
                      ),
                    ],
                  )
                : null,
          ),
        ],
        if (_changes.isNotEmpty && !_hasInvalidEditedDraft && !widget.busy)
          Align(
            alignment: Alignment.centerRight,
            child: OutlinedButton(
              key: const Key('create-item-submit-card-changes'),
              onPressed: widget.onSubmitCardChanges == null
                  ? null
                  : () => widget.onSubmitCardChanges!(_changes),
              child: const Text('送出修改'),
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
              onPressed:
                  widget.busy ||
                      _hasUnsentDraft ||
                      _selectedOperationIds.isEmpty
                  ? null
                  : () => widget.onDecision(
                      approved: true,
                      approvedOperationIds: widget.approval.operations
                          .where(
                            (operation) =>
                                _selectedOperationIds.contains(operation.id),
                          )
                          .map((operation) => operation.id)
                          .toList(growable: false),
                    ),
              child: const Text('確認'),
            ),
          ],
        ),
      ],
    ),
  );
}

String _createItemLabel(OperationView operation) {
  final name = operation.values['name'];
  final quantity = operation.values['quantity'];
  final unit = operation.values['unit'];
  return name is String && quantity is num && unit is String
      ? '$name ${_quantityText(quantity)} $unit'
      : operation.description;
}

String _createItemLocationLabel(OperationView operation) {
  final location = _normalizedLocation(operation.values['location']);
  return '位置：${location ?? '未填'}';
}

String _locationText(Object? value) => _normalizedLocation(value) ?? '';

String? _normalizedLocation(Object? value) {
  final location = value is String ? value.trim() : '';
  return location.isEmpty ? null : location;
}

class _GenericBatchApprovalCard extends StatefulWidget {
  const _GenericBatchApprovalCard({
    required this.approval,
    required this.busy,
    required this.onDecision,
  });

  final ApprovalView approval;
  final bool busy;
  final ApprovalDecisionCallback onDecision;

  @override
  State<_GenericBatchApprovalCard> createState() =>
      _GenericBatchApprovalCardState();
}

class _GenericBatchApprovalCardState extends State<_GenericBatchApprovalCard> {
  late final Set<String> _selectedOperationIds;

  @override
  void initState() {
    super.initState();
    _selectedOperationIds = widget.approval.operations
        .map((operation) => operation.id)
        .toSet();
  }

  @override
  Widget build(BuildContext context) {
    final isFixture = widget.approval.operations.any(
      (operation) => operation.action.isEmpty,
    );
    return Card(
      key: const Key('generic-batch-approval-card'),
      color: Theme.of(context).colorScheme.secondaryContainer,
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(
              isFixture ? '批次操作確認示範' : '確認操作',
              style: Theme.of(context).textTheme.titleMedium,
            ),
            for (final operation in widget.approval.operations)
              CheckboxListTile(
                key: Key('generic-batch-operation-${operation.id}'),
                contentPadding: EdgeInsets.zero,
                value: _selectedOperationIds.contains(operation.id),
                onChanged: widget.busy
                    ? null
                    : (selected) => setState(() {
                        if (selected ?? false) {
                          _selectedOperationIds.add(operation.id);
                        } else {
                          _selectedOperationIds.remove(operation.id);
                        }
                      }),
                title: Text(operation.description),
              ),
            if (isFixture) const Text('此示範不會寫入資料，也不會下單或付款。'),
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
                  onPressed: widget.busy || _selectedOperationIds.isEmpty
                      ? null
                      : () => widget.onDecision(
                          approved: true,
                          approvedOperationIds: widget.approval.operations
                              .where(
                                (operation) => _selectedOperationIds.contains(
                                  operation.id,
                                ),
                              )
                              .map((operation) => operation.id)
                              .toList(growable: false),
                        ),
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
