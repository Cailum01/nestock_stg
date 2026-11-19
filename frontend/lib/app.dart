import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'features/onboarding/domain/household_context.dart';
import 'features/onboarding/domain/identity_controller.dart';
import 'features/onboarding/data/secure_transport_client.dart';
import 'features/onboarding/presentation/identity_gate.dart';
import 'features/agent/domain/agent_client.dart';
import 'features/agent/presentation/agent_home.dart';
import 'features/inventory/inventory_view_client.dart';
import 'features/shopping/shopping_view_client.dart';
import 'features/shopping/commerce_options_client.dart';
import 'shared/data/catalog_client.dart';
import 'shared/config/app_config.dart';
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
    this.identityController,
    this.householdContext,
    this.config = AppConfig.fromEnvironment,
    this.networkClient,
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
  final IdentityController? identityController;
  final HouseholdRequestContext? householdContext;
  final AppConfig config;
  final http.Client? networkClient;

  @override
  Widget build(BuildContext context) {
    final controller = identityController;
    return MaterialApp(
      title: 'Nestock｜窩有',
      theme: buildAppTheme(),
      debugShowCheckedModeBanner: false,
      home: controller == null
          ? Builder(builder: (context) => _buildAgentHome(context, null))
          : IdentityGate(
              controller: controller,
              authenticatedHomeBuilder: (context, session) =>
                  _buildAgentHome(context, session),
            ),
    );
  }

  Widget _buildAgentHome(BuildContext context, IdentityController? session) =>
      AgentHome(
        client: agentClient,
        photoClient: photoClient,
        cameraCapture: cameraCapture,
        speechRecorder: speechRecorder ?? DeviceSpeechRecorder(),
        speechClient:
            speechClient ??
            HttpSpeechClient(
              Uri.parse(config.mediaApiBaseUrl),
              householdContext:
                  householdContext ?? session ?? _UnavailableHouseholdContext(),
              client:
                  networkClient ??
                  SecureTransportClient(allowInsecureLoopback: kDebugMode),
            ),
        photoUpload:
            photoUpload ??
            HttpPhotoUpload(
              config: PhotoNetworkConfig.forApiBase(
                Uri.parse(config.mediaApiBaseUrl),
              ),
              householdContext:
                  householdContext ?? session ?? _UnavailableHouseholdContext(),
              client:
                  networkClient ??
                  SecureTransportClient(allowInsecureLoopback: kDebugMode),
            ),
        inventoryClient: inventoryClient,
        shoppingClient: shoppingClient,
        commerceOptionsClient: commerceOptionsClient,
        appBarActions: const [],
      );
}

class _UnavailableHouseholdContext implements HouseholdRequestContext {
  @override
  HouseholdRequestScope snapshot({String? householdIdOverride}) =>
      throw StateError('Household login is not configured.');
}
