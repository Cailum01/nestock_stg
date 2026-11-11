import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../../../shared/theme/app_theme.dart';
import '../domain/agent_view_data.dart';

enum ConfirmationDraftField { name, quantity, unit, location }

final _quantityEditingPattern = RegExp(r'^(?:\d+(?:\.\d*)?)?$');
final _validQuantityPattern = RegExp(r'^\d+(?:\.\d+)?$');

bool _isValidQuantity(String value) =>
    _validQuantityPattern.hasMatch(value) &&
    (num.tryParse(value)?.isNegative ?? true) == false;

/// A currently focused local Confirmation draft field.
///
/// This remains presentation-only: it provides the existing composer speech
/// flow with a local input target, without exposing a correction protocol.
class ConfirmationDraftFocus {
  const ConfirmationDraftFocus({
    required this.controller,
    required this.focusNode,
    required this.field,
  });

  final TextEditingController controller;
  final FocusNode focusNode;
  final ConfirmationDraftField field;

  bool get isCurrent => focusNode.hasFocus;

  void applyTranscript(String transcript) {
    final next = switch (field) {
      ConfirmationDraftField.quantity => transcript.trim(),
      _ =>
        controller.text.isEmpty ? transcript : '${controller.text} $transcript',
    };
    if (field == ConfirmationDraftField.quantity && !_isValidQuantity(next)) {
      return;
    }
    controller.value = TextEditingValue(
      text: next,
      selection: TextSelection.collapsed(offset: next.length),
    );
  }
}

/// A local, reusable presentation prototype. It does not represent approval
/// persistence, lifecycle, or permission to write.
class ConfirmationCard extends StatefulWidget {
  const ConfirmationCard({
    super.key,
    required this.items,
    this.title = '確認加入庫存',
    this.description,
    this.details = const {},
    this.onCancel,
    this.onConfirm,
    this.onSubmitChanges,
    this.onDraftFocusChanged,
    this.allowExclusion = true,
  });

  final List<ConfirmationItemView> items;
  final String title;
  final String? description;
  final Map<String, dynamic> details;
  final VoidCallback? onCancel;
  final VoidCallback? onConfirm;
  final ValueChanged<List<ConfirmationItemView>>? onSubmitChanges;
  final ValueChanged<ConfirmationDraftFocus?>? onDraftFocusChanged;
  final bool allowExclusion;

  @override
  State<ConfirmationCard> createState() => _ConfirmationCardState();
}

class _ConfirmationCardState extends State<ConfirmationCard> {
  final Set<int> _excluded = {};
  int? _editingIndex;
  late final List<TextEditingController> _names;
  late final List<TextEditingController> _quantities;
  late final List<TextEditingController> _units;
  late final List<TextEditingController> _locations;
  late final List<String> _lastValidQuantities;
  late final List<FocusNode> _nameFocusNodes;
  late final List<FocusNode> _quantityFocusNodes;
  late final List<FocusNode> _unitFocusNodes;
  late final List<FocusNode> _locationFocusNodes;

  String _quantityText(num quantity) => quantity == quantity.truncateToDouble()
      ? quantity.toStringAsFixed(0)
      : quantity.toString();

  @override
  void initState() {
    super.initState();
    _names = [
      for (final item in widget.items) TextEditingController(text: item.name),
    ];
    _quantities = [
      for (final item in widget.items)
        TextEditingController(text: _quantityText(item.quantity)),
    ];
    _units = [
      for (final item in widget.items) TextEditingController(text: item.unit),
    ];
    _locations = [
      for (final item in widget.items)
        TextEditingController(text: item.location),
    ];
    _lastValidQuantities = [
      for (final controller in _quantities) controller.text,
    ];
    for (var index = 0; index < _quantities.length; index++) {
      _quantities[index].addListener(() {
        final value = _quantities[index].text;
        if (_isValidQuantity(value)) _lastValidQuantities[index] = value;
      });
    }
    _nameFocusNodes = [for (final _ in widget.items) FocusNode()];
    _quantityFocusNodes = [for (final _ in widget.items) FocusNode()];
    _unitFocusNodes = [for (final _ in widget.items) FocusNode()];
    _locationFocusNodes = [for (final _ in widget.items) FocusNode()];
    for (final focusNode in [
      ..._nameFocusNodes,
      ..._quantityFocusNodes,
      ..._unitFocusNodes,
      ..._locationFocusNodes,
    ]) {
      focusNode.addListener(_notifyDraftFocus);
    }
  }

  void _notifyDraftFocus() {
    ConfirmationDraftFocus? focused;
    for (var index = 0; index < widget.items.length; index++) {
      if (_nameFocusNodes[index].hasFocus) {
        focused = ConfirmationDraftFocus(
          controller: _names[index],
          focusNode: _nameFocusNodes[index],
          field: ConfirmationDraftField.name,
        );
      } else if (_quantityFocusNodes[index].hasFocus) {
        focused = ConfirmationDraftFocus(
          controller: _quantities[index],
          focusNode: _quantityFocusNodes[index],
          field: ConfirmationDraftField.quantity,
        );
      } else if (_unitFocusNodes[index].hasFocus) {
        focused = ConfirmationDraftFocus(
          controller: _units[index],
          focusNode: _unitFocusNodes[index],
          field: ConfirmationDraftField.unit,
        );
      } else if (_locationFocusNodes[index].hasFocus) {
        focused = ConfirmationDraftFocus(
          controller: _locations[index],
          focusNode: _locationFocusNodes[index],
          field: ConfirmationDraftField.location,
        );
      }
    }
    widget.onDraftFocusChanged?.call(focused);
  }

  void _restoreValidQuantity(int index) {
    if (!_isValidQuantity(_quantities[index].text)) {
      _quantities[index].text = _lastValidQuantities[index];
    }
  }

  void _setEditingIndex(int? index) {
    final previousIndex = _editingIndex;
    if (previousIndex != null && previousIndex != index) {
      _restoreValidQuantity(previousIndex);
    }
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() => _editingIndex = index);
  }

  void _finishEditing(int index) {
    _restoreValidQuantity(index);
    _setEditingIndex(null);
  }

  void _toggleItem(int index) {
    if (!widget.allowExclusion) return;
    if (_excluded.contains(index)) {
      setState(() => _excluded.remove(index));
      return;
    }
    _restoreValidQuantity(index);
    FocusManager.instance.primaryFocus?.unfocus();
    setState(() {
      _excluded.add(index);
      if (_editingIndex == index) _editingIndex = null;
    });
  }

  @override
  void dispose() {
    widget.onDraftFocusChanged?.call(null);
    for (final controller in [
      ..._names,
      ..._quantities,
      ..._units,
      ..._locations,
    ]) {
      controller.dispose();
    }
    for (final focusNode in [
      ..._nameFocusNodes,
      ..._quantityFocusNodes,
      ..._unitFocusNodes,
      ..._locationFocusNodes,
    ]) {
      focusNode
        ..removeListener(_notifyDraftFocus)
        ..dispose();
    }
    super.dispose();
  }

  List<ConfirmationItemView> get _drafts => [
    for (var index = 0; index < widget.items.length; index++)
      ConfirmationItemView(
        name: _names[index].text.trim(),
        quantity:
            num.tryParse(_quantities[index].text) ??
            widget.items[index].quantity,
        unit: _units[index].text.trim(),
        location: _locations[index].text.trim(),
      ),
  ];

  bool get _hasChanges {
    final drafts = _drafts;
    for (var index = 0; index < drafts.length; index++) {
      final item = widget.items[index];
      final draft = drafts[index];
      if (item.name != draft.name ||
          item.quantity != draft.quantity ||
          item.unit != draft.unit ||
          item.location != draft.location) {
        return true;
      }
    }
    return false;
  }

  @override
  Widget build(BuildContext context) => Stack(
    key: const Key('confirmation-card-shell'),
    clipBehavior: Clip.none,
    children: [
      Padding(
        padding: const EdgeInsets.all(26),
        child: Material(
          color: Theme.of(context).colorScheme.surfaceContainerLowest,
          borderRadius: BorderRadius.circular(14),
          child: Padding(
            padding: const EdgeInsets.fromLTRB(18, 18, 18, 14),
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text(
                  widget.title,
                  style: Theme.of(context).textTheme.titleMedium,
                ),
                if (widget.description != null) ...[
                  const SizedBox(height: 4),
                  Text(widget.description!),
                ],
                const SizedBox(height: 8),
                for (var index = 0; index < widget.items.length; index++)
                  _ConfirmationItem(
                    key: ValueKey('confirmation-item-$index'),
                    item: widget.items[index],
                    excluded: _excluded.contains(index),
                    editing: _editingIndex == index,
                    nameController: _names[index],
                    quantityController: _quantities[index],
                    unitController: _units[index],
                    locationController: _locations[index],
                    nameFocusNode: _nameFocusNodes[index],
                    quantityFocusNode: _quantityFocusNodes[index],
                    unitFocusNode: _unitFocusNodes[index],
                    locationFocusNode: _locationFocusNodes[index],
                    onToggle: () => _toggleItem(index),
                    onEdit: () => _setEditingIndex(index),
                    onFinishEdit: () => _finishEditing(index),
                    onChanged: () => setState(() {}),
                    index: index,
                    allowExclusion: widget.allowExclusion,
                  ),
                if (widget.items.isEmpty)
                  for (final entry in widget.details.entries)
                    Padding(
                      padding: const EdgeInsets.symmetric(vertical: 2),
                      child: Text('${entry.key}：${entry.value}'),
                    ),
                const SizedBox(height: 10),
                Row(
                  mainAxisAlignment: MainAxisAlignment.end,
                  children: [
                    TextButton(
                      key: const Key('confirmation-cancel'),
                      onPressed: widget.onCancel,
                      child: const Text('取消'),
                    ),
                    const SizedBox(width: 8),
                    if (widget.onSubmitChanges != null && _hasChanges) ...[
                      OutlinedButton(
                        key: const Key('confirmation-submit-changes'),
                        onPressed: () => widget.onSubmitChanges!(_drafts),
                        child: const Text('送出修改'),
                      ),
                      const SizedBox(width: 8),
                    ],
                    FilledButton(
                      key: const Key('confirmation-confirm'),
                      onPressed: widget.onConfirm,
                      child: const Text('確認'),
                    ),
                  ],
                ),
              ],
            ),
          ),
        ),
      ),
      const _ConfirmationBorder(),
    ],
  );
}

class _ConfirmationItem extends StatelessWidget {
  const _ConfirmationItem({
    super.key,
    required this.item,
    required this.excluded,
    required this.editing,
    required this.nameController,
    required this.quantityController,
    required this.unitController,
    required this.locationController,
    required this.nameFocusNode,
    required this.quantityFocusNode,
    required this.unitFocusNode,
    required this.locationFocusNode,
    required this.onToggle,
    required this.onEdit,
    required this.onFinishEdit,
    required this.onChanged,
    required this.index,
    required this.allowExclusion,
  });

  final ConfirmationItemView item;
  final bool excluded;
  final bool editing;
  final TextEditingController nameController;
  final TextEditingController quantityController;
  final TextEditingController unitController;
  final TextEditingController locationController;
  final FocusNode nameFocusNode;
  final FocusNode quantityFocusNode;
  final FocusNode unitFocusNode;
  final FocusNode locationFocusNode;
  final VoidCallback onToggle;
  final VoidCallback onEdit;
  final VoidCallback onFinishEdit;
  final VoidCallback onChanged;
  final int index;
  final bool allowExclusion;

  InputDecoration _inlineDecoration(BuildContext context) => InputDecoration(
    isDense: true,
    border: InputBorder.none,
    enabledBorder: InputBorder.none,
    focusedBorder: UnderlineInputBorder(
      borderSide: BorderSide(color: Theme.of(context).colorScheme.primary),
    ),
    contentPadding: EdgeInsets.zero,
  );

  @override
  Widget build(BuildContext context) {
    final nameStyle = Theme.of(context).textTheme.titleSmall
        ?.copyWith(fontWeight: FontWeight.w600);
    final detailStyle = Theme.of(context).textTheme.bodyMedium;
    return Opacity(
      opacity: excluded ? .52 : 1,
      child: Semantics(
        key: Key(
          excluded
              ? 'confirmation-item-excluded-$index'
              : 'confirmation-item-included-$index',
        ),
        label: excluded ? '${item.name}，已排除' : '${item.name}，已包含',
        child: Material(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(10),
          child: InkWell(
            key: Key('confirmation-edit-target-$index'),
            onTap: excluded ? null : onEdit,
            borderRadius: BorderRadius.circular(10),
            child: Container(
              key: Key('confirmation-item-block-$index'),
              margin: const EdgeInsets.symmetric(vertical: 4),
              padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 10),
              decoration: BoxDecoration(
                color: AppTokens.paleBlue,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Stack(
                children: [
                  Padding(
                    padding: EdgeInsets.only(right: excluded ? 44 : 88),
                    child: Column(
                      mainAxisSize: MainAxisSize.min,
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        editing
                            ? TextField(
                                key: Key('confirmation-name-$index'),
                                controller: nameController,
                                focusNode: nameFocusNode,
                                style: nameStyle,
                                onChanged: (_) => onChanged(),
                                decoration: _inlineDecoration(context),
                              )
                            : Text(nameController.text, style: nameStyle),
                        const SizedBox(height: 3),
                        if (editing)
                          Wrap(
                            crossAxisAlignment: WrapCrossAlignment.center,
                            spacing: 8,
                            runSpacing: 4,
                            children: [
                              const Text('數量'),
                              SizedBox(
                                width: 44,
                                child: TextField(
                                  key: Key('confirmation-quantity-$index'),
                                  controller: quantityController,
                                  focusNode: quantityFocusNode,
                                  style: detailStyle,
                                  onChanged: (_) => onChanged(),
                                  keyboardType:
                                      const TextInputType.numberWithOptions(
                                        decimal: true,
                                      ),
                                  inputFormatters: [
                                    TextInputFormatter.withFunction(
                                      (oldValue, newValue) =>
                                          _quantityEditingPattern.hasMatch(
                                            newValue.text,
                                          )
                                          ? newValue
                                          : oldValue,
                                    ),
                                  ],
                                  decoration: _inlineDecoration(context),
                                ),
                              ),
                              const Text('單位'),
                              SizedBox(
                                width: 72,
                                child: TextField(
                                  key: Key('confirmation-unit-$index'),
                                  controller: unitController,
                                  focusNode: unitFocusNode,
                                  style: detailStyle,
                                  onChanged: (_) => onChanged(),
                                  decoration: _inlineDecoration(context),
                                ),
                              ),
                            ],
                          )
                        else
                          Text(
                            '數量　　${quantityController.text} ${unitController.text}',
                            style: detailStyle,
                          ),
                        const SizedBox(height: 2),
                        Row(
                          children: [
                            const Icon(Icons.location_on_outlined, size: 16),
                            const SizedBox(width: 4),
                            Expanded(
                              child: editing
                                  ? Semantics(
                                      label: '位置',
                                      child: TextField(
                                        key: Key(
                                          'confirmation-location-$index',
                                        ),
                                        controller: locationController,
                                        focusNode: locationFocusNode,
                                        style: detailStyle,
                                        onChanged: (_) => onChanged(),
                                        decoration: _inlineDecoration(context),
                                      ),
                                    )
                                  : Text(
                                      locationController.text,
                                      style: detailStyle,
                                    ),
                            ),
                          ],
                        ),
                      ],
                    ),
                  ),
                  Positioned(
                    top: -6,
                    right: 0,
                    child: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        if (!excluded)
                          IconButton(
                            key: Key(
                              editing
                                  ? 'confirmation-finish-edit-$index'
                                  : 'confirmation-edit-$index',
                            ),
                            tooltip: editing ? '完成本地編輯' : '編輯此項',
                            constraints: const BoxConstraints(
                              minWidth: 44,
                              minHeight: 44,
                            ),
                            icon: Icon(
                              editing ? Icons.check : Icons.edit_outlined,
                              size: 18,
                            ),
                            onPressed: editing ? onFinishEdit : onEdit,
                          ),
                        if (allowExclusion)
                          IconButton(
                            key: Key(
                              excluded
                                  ? 'confirmation-include-$index'
                                  : 'confirmation-exclude-$index',
                            ),
                            tooltip: excluded ? '重新加入此項' : '排除此項',
                            constraints: const BoxConstraints(
                              minWidth: 44,
                              minHeight: 44,
                            ),
                            icon: Icon(
                              excluded ? Icons.undo : Icons.close,
                              size: 18,
                            ),
                            onPressed: onToggle,
                          ),
                      ],
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
}

class _ConfirmationBorder extends StatelessWidget {
  const _ConfirmationBorder();

  static const _strokeThickness = 22.0;
  // Adjacent bounds meet at their corners without overlapping. The artwork
  // itself provides the intentionally open visual corner.
  static const _cornerInset = _strokeThickness;

  @override
  Widget build(BuildContext context) => Positioned.fill(
    child: IgnorePointer(
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          Positioned(
            key: const Key('confirmation-border-top'),
            left: _cornerInset,
            right: _cornerInset,
            top: 0,
            height: _strokeThickness,
            child: Image.asset(
              'assets/images/ui/confirmation_border_horizontal.png',
              fit: BoxFit.fill,
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-bottom'),
            left: _cornerInset,
            right: _cornerInset,
            bottom: 0,
            height: _strokeThickness,
            child: Transform.rotate(
              key: const Key('confirmation-border-bottom-rotate'),
              angle: math.pi,
              child: Image.asset(
                'assets/images/ui/confirmation_border_horizontal.png',
                fit: BoxFit.fill,
              ),
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-left'),
            left: 0,
            top: _cornerInset,
            bottom: _cornerInset,
            width: _strokeThickness,
            child: Image.asset(
              'assets/images/ui/confirmation_border_vertical.png',
              fit: BoxFit.fill,
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-right'),
            right: 0,
            top: _cornerInset,
            bottom: _cornerInset,
            width: _strokeThickness,
            child: Transform.rotate(
              key: const Key('confirmation-border-right-rotate'),
              angle: math.pi,
              child: Image.asset(
                'assets/images/ui/confirmation_border_vertical.png',
                fit: BoxFit.fill,
              ),
            ),
          ),
        ],
      ),
    ),
  );
}
