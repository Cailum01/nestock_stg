import 'package:flutter/material.dart';

import '../../shared/data/catalog_client.dart';
import '../../shared/widgets/item_list.dart';

class InventoryScreen extends StatelessWidget {
  const InventoryScreen({super.key, required this.catalog});
  final CatalogClient catalog;
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('庫存')),
    body: ItemList(load: catalog.inventory, emptyMessage: '目前沒有庫存品項。'),
  );
}
