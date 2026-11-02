import 'package:flutter/material.dart';

import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_view_client.dart';
import 'features/shopping/shopping_view_client.dart';
import 'shared/data/catalog_client.dart';
import 'shared/theme/app_theme.dart';
import 'features/photo/photo_client.dart';
import 'features/photo/camera_capture.dart';
import 'features/photo/photo_upload.dart';

import 'package:flutter/foundation.dart';

class NestockApp extends StatelessWidget {
  const NestockApp({
    super.key,
    required this.agentClient,
    required this.catalog,
    this.photoClient = const UnavailablePhotoClient(),
    this.cameraCapture = const DeviceCameraCapture(),
    this.photoUpload,
    this.inventoryClient = const DemoInventoryViewClient(),
    this.shoppingClient = const DemoShoppingViewClient(),
  });
  final AgentClient agentClient;
  final CatalogClient catalog;
  final PhotoClient photoClient;
  final CameraCapture cameraCapture;
  final PhotoUpload? photoUpload;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nestock｜窩有',
    theme: buildAppTheme(),
    home: AgentHome(
      client: agentClient,
      photoClient: photoClient,
      cameraCapture: cameraCapture,
      photoUpload:
          photoUpload ??
          HttpPhotoUpload(
            config: PhotoNetworkConfig(
              apiBase: Uri.parse(
                const String.fromEnvironment(
                  'MEDIA_API_BASE',
                  defaultValue: 'http://10.0.2.2:8000',
                ),
              ),
              emulatorHost:
                  !kIsWeb && defaultTargetPlatform == TargetPlatform.android
                  ? '10.0.2.2'
                  : null,
            ),
          ),
      inventoryClient: inventoryClient,
      shoppingClient: shoppingClient,
    ),
  );
}
