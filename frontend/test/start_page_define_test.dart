import 'package:flutter_test/flutter_test.dart';
import 'package:http/testing.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/onboarding/data/identity_api.dart';
import 'package:nestock/features/onboarding/data/secure_credential_store.dart';
import 'package:nestock/features/onboarding/domain/identity_controller.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class _EmptyCredentialStore implements SecureCredentialStore {
  @override
  Future<String?> readBearerCredential() async => null;

  @override
  Future<void> writeBearerCredential(String credential) async {}

  @override
  Future<void> deleteBearerCredential() async {}
}

void main() {
  const configuredStartPage = String.fromEnvironment('NESTOCK_START_PAGE');

  testWidgets('compile-time start page define selects Connect only when set', (
    tester,
  ) async {
    final controller = IdentityController(
      api: IdentityApi(
        baseUrl: Uri.parse('https://api.example.test'),
        client: MockClient((_) async => throw StateError('Unexpected request')),
      ),
      credentialStore: _EmptyCredentialStore(),
    );
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(),
        catalog: const DemoCatalog(),
        identityController: controller,
      ),
    );
    await tester.pumpAndSettle();

    if (configuredStartPage == 'connect_household') {
      expect(find.text('Connect a household'), findsOneWidget);
      expect(find.text('Continue with Google'), findsNothing);
    } else {
      expect(find.text('Connect a household'), findsNothing);
      expect(find.text('Continue with Google'), findsOneWidget);
    }
  });
}
