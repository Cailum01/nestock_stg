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
  final _nameController = TextEditingController();
  final _localItems = <ShoppingViewItem>[];
  bool _adding = false;
  String? _addError;

  void _openAdd() => setState(() {
    _adding = true;
    _addError = null;
  });

  void _cancelAdd() => setState(() {
    _adding = false;
    _addError = null;
    _nameController.clear();
  });

  void _addLocalItem() {
    final name = _nameController.text.trim();
    if (name.isEmpty) {
      setState(() => _addError = '請輸入商品名稱。');
      return;
    }
    setState(() {
      // This display projection still requires quantity and unit, but intent-only
      // local rows deliberately leave both blank until a future intake flow.
      _localItems.add(ShoppingViewItem(name, '', '', 'pending'));
      _adding = false;
      _addError = null;
      _nameController.clear();
    });
  }

  @override
  void dispose() {
    _nameController.dispose();
    super.dispose();
  }

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
                final data = [...snapshot.data!, ..._localItems];
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
                            );
                          },
                        ),
                      ),
                    if (_adding) ...[
                      const SizedBox(height: 8),
                      TextField(
                        key: const Key('shopping-add-name'),
                        controller: _nameController,
                        textInputAction: TextInputAction.done,
                        decoration: const InputDecoration(labelText: '商品名稱'),
                      ),
                      if (_addError != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Text(
                            _addError!,
                            style: TextStyle(
                              color: Theme.of(context).colorScheme.error,
                            ),
                          ),
                        ),
                      Align(
                        alignment: Alignment.centerRight,
                        child: Wrap(
                          spacing: 8,
                          children: [
                            TextButton(
                              onPressed: _cancelAdd,
                              child: const Text('取消'),
                            ),
                            FilledButton(
                              onPressed: _addLocalItem,
                              child: const Text('加入'),
                            ),
                          ],
                        ),
                      ),
                    ] else
                      Align(
                        alignment: Alignment.centerLeft,
                        child: TextButton.icon(
                          key: const Key('shopping-add-entry'),
                          onPressed: _openAdd,
                          icon: const Icon(Icons.add),
                          label: const Text('新增購物項目'),
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
