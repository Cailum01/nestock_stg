import 'package:flutter/material.dart';

import 'shopping_view_client.dart';

class ShoppingOverlay extends StatefulWidget {
  const ShoppingOverlay({
    super.key,
    required this.client,
    required this.onClose,
  });
  final ShoppingViewClient client;
  final VoidCallback onClose;
  @override
  State<ShoppingOverlay> createState() => _ShoppingOverlayState();
}

class _ShoppingOverlayState extends State<ShoppingOverlay> {
  late final Future<List<ShoppingViewItem>> items = widget.client.load();
  @override
  Widget build(BuildContext context) => Material(
    key: const Key('shopping-overlay'),
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
              const Expanded(child: Text('購物清單')),
              IconButton(
                onPressed: widget.onClose,
                icon: const Icon(Icons.close),
                tooltip: '關閉購物清單',
              ),
            ],
          ),
          Flexible(
            child: FutureBuilder<List<ShoppingViewItem>>(
              future: items,
              builder: (context, snapshot) {
                if (snapshot.hasError) return const Text('無法載入購物清單。');
                if (!snapshot.hasData) return const CircularProgressIndicator();
                final data = snapshot.data!;
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      '待購 ${data.where((item) => item.status == 'pending').length} 件',
                    ),
                    if (data.isEmpty)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: Text('購物清單目前是空的。'),
                      )
                    else
                      Flexible(
                        child: ListView.builder(
                          key: const Key('shopping-items'),
                          shrinkWrap: true,
                          itemCount: data.length,
                          itemBuilder: (context, index) {
                            final item = data[index];
                            return ListTile(
                              dense: true,
                              title: Text(item.name),
                              subtitle: Text(
                                item.status == 'completed' ? '已完成' : '待購買',
                              ),
                              trailing: Text('${item.quantity} ${item.unit}'),
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
