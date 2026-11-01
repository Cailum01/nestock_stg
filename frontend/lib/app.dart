import 'package:flutter/material.dart';

import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_screen.dart';
import 'features/shopping/shopping_screen.dart';
import 'shared/data/catalog_client.dart';
import 'shared/theme/app_theme.dart';
import 'features/photo/photo_client.dart';

class NestockApp extends StatelessWidget {
  const NestockApp({
    super.key,
    required this.agentClient,
    required this.catalog,
    this.photoClient = const UnavailablePhotoClient(),
  });
  final AgentClient agentClient;
  final CatalogClient catalog;
  final PhotoClient photoClient;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nestock｜窩有',
    theme: buildAppTheme(),
    home: AgentHome(
      client: agentClient,
      photoClient: photoClient,
      openInventory: (context) => Navigator.of(context).push<void>(
        MaterialPageRoute(builder: (_) => InventoryScreen(catalog: catalog)),
      ),
      openShopping: (context) => Navigator.of(context).push<void>(
        MaterialPageRoute(builder: (_) => ShoppingScreen(catalog: catalog)),
      ),
    ),
  );
}
