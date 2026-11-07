import 'package:flutter/material.dart';

/// Read-only observations; none of these values imply an inventory write.
class VisionResultItems extends StatelessWidget {
  const VisionResultItems({super.key, required this.data});
  final Map<String, dynamic> data;

  @override
  Widget build(BuildContext context) {
    final items = data['items'];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('僅辨識，尚未加入庫存', style: Theme.of(context).textTheme.labelMedium),
        if (items is List)
          for (final item in items)
            if (item is Map) _VisionItem(item: item),
      ],
    );
  }
}

class _VisionItem extends StatelessWidget {
  const _VisionItem({required this.item});
  final Map item;

  String? _text(dynamic value) =>
      value is String && value.trim().isNotEmpty ? value.trim() : null;

  @override
  Widget build(BuildContext context) {
    final name = _text(item['name']) ?? _text(item['seen_as']);
    if (name == null) return const SizedBox.shrink();
    final unit = _text(item['unit']);
    final quantity = item['quantity'];
    final quantityText = quantity is num && quantity.isFinite
        ? (quantity == quantity.truncateToDouble()
              ? quantity.toStringAsFixed(0)
              : quantity.toString())
        : null;
    final expiry = _text(item['expiry_date']);
    final match = item['match'];
    final matchMap = match is Map ? match : null;
    final choices = matchMap?['choices'];
    final names = choices is List
        ? choices
              .whereType<Map>()
              .map((e) => _text(e['item_name']))
              .whereType<String>()
              .toList()
        : <String>[];
    final householdItemName = _text(matchMap?['item_name']);
    final matchMethod = _text(matchMap?['method']);
    return Padding(
      padding: const EdgeInsets.only(top: 8),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(name, style: Theme.of(context).textTheme.titleSmall),
          if (quantityText != null)
            Text(
              '$quantityText${unit == null ? '' : ' $unit'}${unit == null || item['unit_confirmed'] != true ? '（單位待確認）' : ''}',
            ),
          if (quantityText == null && unit != null)
            Text('單位：$unit${item['unit_confirmed'] == true ? '' : '（待確認）'}'),
          if (expiry != null) Text('期限：$expiry'),
          if (names.isNotEmpty) Text('可能符合：${names.join('、')}'),
          if (names.isEmpty &&
              matchMethod != 'none' &&
              householdItemName != null)
            Text('家中品項：$householdItemName'),
          if (item['low_confidence'] == true) const Text('辨識結果待確認'),
        ],
      ),
    );
  }
}
