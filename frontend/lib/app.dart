import 'package:flutter/material.dart';

import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_view_client.dart';
import 'features/shopping/shopping_view_client.dart';
import 'shared/data/catalog_client.dart';
import 'shared/theme/app_theme.dart';
import 'features/photo/photo_client.dart';

class NestockApp extends StatelessWidget {
  const NestockApp({
    super.key,
    required this.agentClient,
    required this.catalog,
    this.photoClient = const UnavailablePhotoClient(),
    this.inventoryClient = const DemoInventoryViewClient(),
    this.shoppingClient = const DemoShoppingViewClient(),
  });
  final AgentClient agentClient;
  final CatalogClient catalog;
  final PhotoClient photoClient;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nestock｜窩有',
    theme: buildAppTheme(),
    home: AgentHome(
      client: agentClient,
      photoClient: photoClient,
      inventoryClient: inventoryClient,
      shoppingClient: shoppingClient,
    ),
  );
}
