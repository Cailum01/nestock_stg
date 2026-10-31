import 'package:flutter/material.dart';

import '../data/catalog_client.dart';
import 'async_state.dart';

class ItemList extends StatefulWidget {
  const ItemList({super.key, required this.load, required this.emptyMessage});
  final Future<List<ItemSummary>> Function() load;
  final String emptyMessage;
  @override
  State<ItemList> createState() => _ItemListState();
}

class _ItemListState extends State<ItemList> {
  late Future<List<ItemSummary>> _items;
  @override
  void initState() {
    super.initState();
    _items = widget.load();
  }

  @override
  void didUpdateWidget(covariant ItemList oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.load != widget.load) _items = widget.load();
  }

  @override
  Widget build(BuildContext context) => FutureBuilder<List<ItemSummary>>(
    future: _items,
    builder: (context, snapshot) {
      if (snapshot.connectionState != ConnectionState.done) {
        return const LoadingState();
      }
      if (snapshot.hasError) {
        return Center(
          child: ErrorState(
            onRetry: () => setState(() {
              _items = widget.load();
            }),
          ),
        );
      }
      final items = snapshot.data ?? [];
      if (items.isEmpty) return Center(child: Text(widget.emptyMessage));
      return ListView(
        children: [
          const Padding(padding: EdgeInsets.all(16), child: Text('唯讀示範資料')),
          for (final item in items)
            ListTile(title: Text(item.name), subtitle: Text(item.detail)),
        ],
      );
    },
  );
}
