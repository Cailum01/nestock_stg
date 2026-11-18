import 'package:flutter/material.dart';

import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_view_client.dart';
import 'features/shopping/shopping_view_client.dart';
import 'features/shopping/commerce_options_client.dart';
import 'shared/data/catalog_client.dart';
import 'shared/theme/app_theme.dart';
import 'features/photo/photo_client.dart';
import 'features/photo/camera_capture.dart';
import 'features/photo/photo_upload.dart';
import 'features/speech/speech_client.dart';

class NestockApp extends StatelessWidget {
  const NestockApp({
    super.key,
    required this.agentClient,
    required this.catalog,
    this.photoClient = const UnavailablePhotoClient(),
    this.cameraCapture = const DeviceCameraCapture(),
    this.photoUpload,
    this.speechRecorder,
    this.speechClient,
    this.inventoryClient = const DemoInventoryViewClient(),
    this.shoppingClient = const DemoShoppingViewClient(),
    this.commerceOptionsClient = const UnavailableCommerceOptionsClient(),
  });
  final AgentClient agentClient;
  final CatalogClient catalog;
  final PhotoClient photoClient;
  final CameraCapture cameraCapture;
  final PhotoUpload? photoUpload;
  final SpeechRecorder? speechRecorder;
  final SpeechClient? speechClient;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;
  final CommerceOptionsClient commerceOptionsClient;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'Nestock｜窩有',
    theme: buildAppTheme(),
    home: AgentHome(
      client: agentClient,
      photoClient: photoClient,
      cameraCapture: cameraCapture,
      speechRecorder: speechRecorder ?? DeviceSpeechRecorder(),
      speechClient:
          speechClient ??
          HttpSpeechClient(
            Uri.parse(
              const String.fromEnvironment(
                'MEDIA_API_BASE',
                defaultValue: 'http://10.0.2.2:8000',
              ),
            ),
          ),
      photoUpload:
          photoUpload ??
          HttpPhotoUpload(
            config: PhotoNetworkConfig.forApiBase(
              Uri.parse(
                const String.fromEnvironment(
                  'MEDIA_API_BASE',
                  defaultValue: 'http://10.0.2.2:8000',
                ),
              ),
            ),
          ),
      inventoryClient: inventoryClient,
      shoppingClient: shoppingClient,
      commerceOptionsClient: commerceOptionsClient,
    ),
  );
}
