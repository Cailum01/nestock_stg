import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart' show ScrollCacheExtent;
import 'package:flutter/services.dart';

/// Display data and dates are local mock presentation state only.
class AgentWorkSurface extends StatefulWidget {
  const AgentWorkSurface({
    super.key,
    required this.items,
    required this.onClose,
    this.embedded = false,
  });
  final List<({String name, String quantity, String location})> items;
  final VoidCallback onClose;
  final bool embedded;
  @override
  State<AgentWorkSurface> createState() => _AgentWorkSurfaceState();
}

class _AgentWorkSurfaceState extends State<AgentWorkSurface> {
  final Map<int, DateTime> dates = {};
  late List<int> remaining;
  final Map<int, TextEditingController> quantities = {};
  final Map<int, TextEditingController> locations = {};
  final Map<int, String> units = {};

  void initializeItems() {
    remaining = List.generate(widget.items.length, (index) => index);
    for (final id in remaining) {
      final parts = widget.items[id].quantity.trim().split(RegExp(r'\s+'));
      quantities[id] = TextEditingController(text: parts.first);
      units[id] = parts.skip(1).join(' ');
      locations[id] = TextEditingController(text: widget.items[id].location);
    }
  }

  void disposeInputs() {
    for (final controller in [...quantities.values, ...locations.values]) {
      controller.dispose();
    }
    quantities.clear();
    locations.clear();
    units.clear();
  }

  @override
  void initState() {
    super.initState();
    initializeItems();
  }

  @override
  void didUpdateWidget(covariant AgentWorkSurface oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (!identical(oldWidget.items, widget.items)) {
      disposeInputs();
      initializeItems();
      dates.clear();
    }
  }

  @override
  void dispose() {
    disposeInputs();
    super.dispose();
  }

  Future<void> selectDate(int index) async {
    final now = DateTime.now();
    final selected = await showDatePicker(
      context: context,
      initialDate: dates[index] ?? now,
      firstDate: DateTime(2000),
      lastDate: DateTime(2100),
    );
    if (selected != null && mounted && remaining.contains(index)) {
      setState(() => dates[index] = selected);
    }
  }

  @override
  Widget build(BuildContext context) {
    final content = Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            const SizedBox(width: 16),
            Expanded(
              child: Text(
                '辨識到 ${remaining.length} 件物品',
                style: Theme.of(context).textTheme.titleMedium,
              ),
            ),
            if (!widget.embedded)
              IconButton(
                onPressed: widget.onClose,
                icon: const Icon(Icons.close),
                tooltip: '關閉',
              ),
          ],
        ),
        if (remaining.isEmpty)
          const Padding(padding: EdgeInsets.all(16), child: Text('目前沒有待確認的物品。'))
        else if (widget.embedded)
          Column(
            children: List.generate(
              remaining.length,
              (index) => _item(context, index),
            ),
          )
        else
          Flexible(
            child: ListView.builder(
              key: const Key('work-items'),
              scrollCacheExtent: const ScrollCacheExtent.pixels(1200),
              shrinkWrap: true,
              itemCount: remaining.length,
              itemBuilder: _item,
            ),
          ),
        if (remaining.isNotEmpty)
          FilledButton(
            onPressed:
                remaining.every((id) {
                  final quantity = double.tryParse(quantities[id]!.text);
                  return quantity != null && quantity.isFinite && quantity > 0;
                })
                ? () => ScaffoldMessenger.of(context).showSnackBar(
                    const SnackBar(content: Text('示範操作，沒有儲存任何變更。')),
                  )
                : null,
            child: const Text('確認全部入庫'),
          ),
        const SizedBox(height: 8),
      ],
    );
    if (widget.embedded) return content;
    return Material(
      key: const Key('work-surface'),
      color: Theme.of(context).colorScheme.surface,
      elevation: 4,
      borderRadius: const BorderRadius.vertical(top: Radius.circular(20)),
      child: content,
    );
  }

  Widget _item(BuildContext context, int index) {
    final itemId = remaining[index];
    final item = widget.items[itemId];
    final date = dates[itemId];
    final dateText = date == null
        ? '選擇日期（選填）'
        : '${date.year.toString().padLeft(4, '0')}-${date.month.toString().padLeft(2, '0')}-${date.day.toString().padLeft(2, '0')}';
    return Card(
      key: ValueKey('vision-item-$itemId'),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 6, 12, 4),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisSize: MainAxisSize.min,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    item.name,
                    style: Theme.of(context).textTheme.titleSmall,
                  ),
                ),
                IconButton(
                  tooltip: '移除此項',
                  visualDensity: VisualDensity.compact,
                  constraints: const BoxConstraints(
                    minWidth: 40,
                    minHeight: 40,
                  ),
                  padding: EdgeInsets.zero,
                  icon: const Icon(Icons.close, size: 18),
                  onPressed: () => setState(() {
                    remaining.remove(itemId);
                    dates.remove(itemId);
                  }),
                ),
              ],
            ),
            if ((double.tryParse(quantities[itemId]!.text) ?? 0) <= 0)
              Text(
                '請輸入大於零的數量',
                style: TextStyle(color: Theme.of(context).colorScheme.error),
              ),
            Row(
              children: [
                const Text('數量'),
                const SizedBox(width: 4),
                SizedBox(
                  width: 55,
                  child: TextField(
                    key: ValueKey('quantity-$itemId'),
                    controller: quantities[itemId],
                    keyboardType: const TextInputType.numberWithOptions(
                      decimal: true,
                    ),
                    inputFormatters: [
                      TextInputFormatter.withFunction(
                        (oldValue, newValue) =>
                            RegExp(r'^\d*\.?\d*$').hasMatch(newValue.text)
                            ? newValue
                            : oldValue,
                      ),
                    ],
                    onChanged: (_) => setState(() {}),
                    decoration: const InputDecoration(
                      isDense: true,
                      contentPadding: EdgeInsets.all(6),
                    ),
                  ),
                ),
                const SizedBox(width: 4),
                Text(units[itemId]!),
                const SizedBox(width: 8),
                const Icon(Icons.location_on_outlined, size: 16),
                const SizedBox(width: 4),
                Expanded(
                  child: Semantics(
                    label: '位置',
                    child: TextField(
                      key: ValueKey('location-$itemId'),
                      controller: locations[itemId],
                      decoration: const InputDecoration(
                        isDense: true,
                        contentPadding: EdgeInsets.all(6),
                      ),
                    ),
                  ),
                ),
              ],
            ),
            Row(
              children: [
                const Text('效期'),
                const SizedBox(width: 8),
                Expanded(
                  child: TextButton.icon(
                    onPressed: () => selectDate(itemId),
                    icon: const Icon(Icons.calendar_today_outlined, size: 16),
                    label: Text(dateText),
                    style: TextButton.styleFrom(
                      alignment: Alignment.centerLeft,
                      minimumSize: const Size(0, 36),
                      padding: EdgeInsets.zero,
                    ),
                  ),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
