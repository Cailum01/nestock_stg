import 'package:flutter/material.dart';

import 'inventory_view_client.dart';

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
              const Expanded(child: Text('我的庫存')),
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
                final visible = all
                    .where(
                      (item) =>
                          item.name.toLowerCase().contains(query) ||
                          item.aliases.any(
                            (alias) => alias.toLowerCase().contains(query),
                          ),
                    )
                    .toList();
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text('顯示 ${visible.length} 件'),
                    Flexible(
                      child: visible.isEmpty
                          ? Padding(
                              padding: const EdgeInsets.all(16),
                              child: Text(
                                all.isEmpty ? '目前沒有庫存品項。' : '找不到符合的物品。',
                              ),
                            )
                          : ListView.builder(
                              shrinkWrap: true,
                              itemCount: visible.length,
                              itemBuilder: (context, index) {
                                final item = visible[index];
                                final date = item.expiry;
                                return ListTile(
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
                                          Text('${item.quantity} ${item.unit}'),
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
