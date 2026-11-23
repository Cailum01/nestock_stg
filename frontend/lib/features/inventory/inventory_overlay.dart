import 'package:flutter/material.dart';

import 'inventory_view_client.dart';

enum InventorySortMode { sourceOrder, location, expiry, name }

String _inventorySortLabel(InventorySortMode mode) => switch (mode) {
  InventorySortMode.sourceOrder => '預設',
  InventorySortMode.location => '位置',
  InventorySortMode.expiry => '效期',
  InventorySortMode.name => '名稱',
};

class InventoryOverlay extends StatefulWidget {
  const InventoryOverlay({
    super.key,
    required this.client,
    required this.onClose,
  });
  final InventoryViewClient client;
  final VoidCallback onClose;
  @override
  State<InventoryOverlay> createState() => _InventoryOverlayState();
}

class _InventoryOverlayState extends State<InventoryOverlay> {
  late final Future<List<InventoryViewItem>> items = widget.client.load();
  String query = '';
  InventorySortMode _sortMode = InventorySortMode.sourceOrder;

  List<InventoryViewItem> _visibleItems(List<InventoryViewItem> all) {
    final visible = all
        .where(
          (item) =>
              item.name.toLowerCase().contains(query) ||
              item.aliases.any((alias) => alias.toLowerCase().contains(query)),
        )
        .toList();
    switch (_sortMode) {
      case InventorySortMode.sourceOrder:
        return visible;
      case InventorySortMode.location:
        visible.sort(_compareByLocation);
      case InventorySortMode.expiry:
        visible.sort(_compareByExpiry);
      case InventorySortMode.name:
        visible.sort(_compareByName);
    }
    return visible;
  }

  int _compareByLocation(InventoryViewItem left, InventoryViewItem right) {
    final leftLocation = _normalizedLocation(left.location);
    final rightLocation = _normalizedLocation(right.location);
    if (leftLocation == null && rightLocation != null) return 1;
    if (leftLocation != null && rightLocation == null) return -1;
    if (leftLocation != null && rightLocation != null) {
      final byLocation = leftLocation.compareTo(rightLocation);
      if (byLocation != 0) return byLocation;
    }
    return _compareByName(left, right);
  }

  int _compareByExpiry(InventoryViewItem left, InventoryViewItem right) {
    final leftExpiry = left.expiry;
    final rightExpiry = right.expiry;
    if (leftExpiry == null && rightExpiry != null) return 1;
    if (leftExpiry != null && rightExpiry == null) return -1;
    if (leftExpiry != null && rightExpiry != null) {
      final byExpiry = leftExpiry.compareTo(rightExpiry);
      if (byExpiry != 0) return byExpiry;
    }
    return _compareByName(left, right);
  }

  int _compareByName(InventoryViewItem left, InventoryViewItem right) =>
      _normalizedName(left.name).compareTo(_normalizedName(right.name));

  String? _normalizedLocation(String? value) {
    final normalized = value?.trim().toLowerCase() ?? '';
    return normalized.isEmpty ? null : normalized;
  }

  String _normalizedName(String value) => value.trim().toLowerCase();

  @override
  Widget build(BuildContext context) => Material(
    key: const Key('inventory-overlay'),
    color: Theme.of(context).colorScheme.surface,
    borderRadius: BorderRadius.circular(16),
    elevation: 4,
    child: Padding(
      padding: const EdgeInsets.all(12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '我的庫存',
                  key: const Key('inventory-overlay-title'),
                  style: Theme.of(context).textTheme.titleMedium
                      ?.copyWith(fontWeight: FontWeight.w600),
                ),
              ),
              IconButton(
                onPressed: widget.onClose,
                icon: const Icon(Icons.close),
                tooltip: '關閉庫存',
              ),
            ],
          ),
          TextField(
            key: const Key('inventory-search'),
            onChanged: (value) =>
                setState(() => query = value.trim().toLowerCase()),
            decoration: const InputDecoration(
              isDense: true,
              prefixIcon: Icon(Icons.search),
              hintText: '搜尋物品…',
            ),
          ),
          Flexible(
            child: FutureBuilder<List<InventoryViewItem>>(
              future: items,
              builder: (context, snapshot) {
                if (snapshot.hasError) {
                  return const Center(child: Text('無法載入庫存。'));
                }
                if (!snapshot.hasData) {
                  return const Center(child: CircularProgressIndicator());
                }
                final all = snapshot.data!;
                final visible = _visibleItems(all);
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Row(
                      key: const Key('inventory-summary-row'),
                      children: [
                        Expanded(child: Text('顯示 ${visible.length} 件')),
                        DropdownButton<InventorySortMode>(
                          key: const Key('inventory-sort'),
                          value: _sortMode,
                          isDense: true,
                          onChanged: (mode) {
                            if (mode != null) {
                              setState(() => _sortMode = mode);
                            }
                          },
                          items: [
                            for (final mode in InventorySortMode.values)
                              DropdownMenuItem(
                                value: mode,
                                child: Text(_inventorySortLabel(mode)),
                              ),
                          ],
                        ),
                      ],
                    ),
                    const SizedBox(
                      key: Key('inventory-summary-list-gap'),
                      height: 8,
                    ),
                    Flexible(
                      child: visible.isEmpty
                          ? Padding(
                              padding: const EdgeInsets.all(16),
                              child: Text(
                                all.isEmpty ? '目前沒有庫存品項。' : '找不到符合的物品。',
                              ),
                            )
                          : ListView.separated(
                              shrinkWrap: true,
                              itemCount: visible.length,
                              separatorBuilder: (context, index) =>
                                  const SizedBox(
                                    key: Key('inventory-item-separator'),
                                    height: 2,
                                  ),
                              itemBuilder: (context, index) {
                                final item = visible[index];
                                final date = item.expiry;
                                return ColoredBox(
                                  key: Key('inventory-item-surface-$index'),
                                  color: Theme.of(context)
                                      .colorScheme
                                      .surfaceContainerHighest,
                                  child: ListTile(
                                    dense: true,
                                    title: Text(item.name),
                                    subtitle: Column(
                                      crossAxisAlignment:
                                          CrossAxisAlignment.start,
                                      children: [
                                        Wrap(
                                          crossAxisAlignment:
                                              WrapCrossAlignment.center,
                                          spacing: 4,
                                          children: [
                                            Text(
                                              '${item.quantity} ${item.unit}',
                                            ),
                                            if (item.location != null) ...[
                                              const Icon(
                                                Icons.location_on_outlined,
                                                size: 16,
                                              ),
                                              Text(item.location!),
                                            ],
                                          ],
                                        ),
                                        if (date != null)
                                          Row(
                                            children: [
                                              const Icon(
                                                Icons.calendar_today_outlined,
                                                size: 14,
                                              ),
                                              const SizedBox(width: 4),
                                              Text(
                                                '效期 ${date.year}/${date.month.toString().padLeft(2, '0')}/${date.day.toString().padLeft(2, '0')}',
                                              ),
                                            ],
                                          ),
                                      ],
                                    ),
                                  ),
                                );
                              },
                            ),
                    ),
                  ],
                );
              },
            ),
          ),
        ],
      ),
    ),
  );
}
