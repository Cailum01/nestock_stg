import 'package:flutter/material.dart';

import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_screen.dart';
import 'features/shopping/shopping_screen.dart';
import 'shared/data/catalog_client.dart';

class NestockApp extends StatelessWidget {
  const NestockApp({
    super.key,
    required this.agentClient,
    required this.catalog,
  });
  final AgentClient agentClient;
  final CatalogClient catalog;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nestock｜窩有',
    theme: ThemeData(colorSchemeSeed: Colors.teal, useMaterial3: true),
    home: AgentHome(
      client: agentClient,
      openInventory: (context) => Navigator.of(context).push<void>(
        MaterialPageRoute(builder: (_) => InventoryScreen(catalog: catalog)),
      ),
      openShopping: (context) => Navigator.of(context).push<void>(
        MaterialPageRoute(builder: (_) => ShoppingScreen(catalog: catalog)),
      ),
    ),
  );
}
