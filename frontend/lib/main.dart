import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import 'app.dart';
import 'features/agent/data/mock_agent_client.dart';
import 'features/agent/data/http_agent_client.dart';
import 'features/onboarding/data/identity_api.dart';
import 'features/onboarding/data/google_identity_provider.dart';
import 'features/onboarding/data/secure_transport_client.dart';
import 'features/onboarding/data/secure_credential_store.dart';
import 'features/onboarding/domain/identity_controller.dart';
import 'features/inventory/inventory_view_client.dart';
import 'features/shopping/shopping_view_client.dart';
import 'features/shopping/commerce_options_client.dart';
import 'shared/data/demo_catalog.dart';
import 'shared/config/app_config.dart';

void main() {
  const transport = String.fromEnvironment(
    'AGENT_TRANSPORT',
    defaultValue: 'http',
  );
  const config = AppConfig.fromEnvironment;
  final apiBase = Uri.parse(config.apiBaseUrl);
  final networkClient = SecureTransportClient(
    allowInsecureLoopback: kDebugMode,
  );
  // Local mock builds open the demo home directly and never enroll an install.
  final IdentityController? identityController = transport == 'mock'
      ? null
      : IdentityController(
          api: IdentityApi(baseUrl: apiBase, client: networkClient),
          credentialStore: FlutterSecureCredentialStore(),
          googleIdentityProvider: GoogleSignInIdentityProvider(
            serverClientId: const String.fromEnvironment(
              'NESTOCK_GOOGLE_CLIENT_ID',
            ),
          ),
        );
  const name = String.fromEnvironment(
    'MOCK_SCENARIO',
    defaultValue: 'conversation',
  );
  final scenario =
      MockScenario.values.where((value) => value.name == name).firstOrNull ??
      MockScenario.conversation;
  runApp(
    NestockApp(
      agentClient: transport == 'mock'
          ? MockAgentClient(scenario: scenario)
          : HttpAgentClient(
              baseUrl: apiBase,
              householdContext: _requireIdentity(identityController),
              client: networkClient,
            ),
      catalog: const DemoCatalog(),
      shoppingClient: shoppingViewClientForTransport(
        transport: transport,
        baseUrl: apiBase,
        householdContext: identityController,
      ),
      commerceOptionsClient: commerceOptionsClientForTransport(
        transport: transport,
        baseUrl: apiBase,
        householdContext: identityController,
        client: networkClient,
      ),
      inventoryClient: inventoryViewClientForTransport(
        transport: transport,
        baseUrl: apiBase,
        householdContext: identityController,
      ),
      identityController: identityController,
      householdContext: identityController,
      config: config,
      networkClient: networkClient,
    ),
  );
}

IdentityController _requireIdentity(IdentityController? controller) {
  if (controller == null) {
    throw StateError('HTTP transport requires install identity.');
  }
  return controller;
}
