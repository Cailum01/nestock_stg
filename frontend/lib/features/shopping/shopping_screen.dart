import 'package:flutter/material.dart';

import '../../shared/data/catalog_client.dart';
import '../../shared/widgets/item_list.dart';

class ShoppingScreen extends StatelessWidget {
  const ShoppingScreen({super.key, required this.catalog});
  final CatalogClient catalog;
  @override
  Widget build(BuildContext context) => Scaffold(
    appBar: AppBar(title: const Text('購物清單')),
    body: ItemList(load: catalog.shopping, emptyMessage: '目前沒有待購買品項。'),
  );
}
