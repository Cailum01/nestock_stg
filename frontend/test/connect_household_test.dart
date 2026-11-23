import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/onboarding/data/identity_api.dart';
import 'package:nestock/features/onboarding/data/secure_credential_store.dart';
import 'package:nestock/features/onboarding/domain/identity_controller.dart';
import 'package:nestock/features/onboarding/presentation/identity_gate.dart';

class _MemoryCredentialStore implements SecureCredentialStore {
  @override
  Future<String?> readBearerCredential() async => 'install-token';

  @override
  Future<void> writeBearerCredential(String credential) async {}

  @override
  Future<void> deleteBearerCredential() async {}
}

Future<void> _reveal(WidgetTester tester, Finder target) async {
  await tester.ensureVisible(target);
  await tester.pumpAndSettle();
}

void main() {
  const base = 'https://api.example.test';

  Future<IdentityController> createController({
    required Future<http.Response> Function(http.Request request) respond,
  }) async {
    final controller = IdentityController(
      api: IdentityApi(baseUrl: Uri.parse(base), client: MockClient(respond)),
      credentialStore: _MemoryCredentialStore(),
    );
    await controller.initialize();
    return controller;
  }

  testWidgets('household selector switches active membership on Connect', (
    tester,
  ) async {
    final controller = await createController(
      respond: (request) async => http.Response(
        '{"households":['
        '{"household_id":"alpha","role":"owner"},'
        '{"household_id":"beta","role":"member"}]}',
        200,
      ),
    );
    await tester.pumpWidget(
      MaterialApp(home: HouseholdOnboarding(controller: controller)),
    );

    expect(controller.selectedHouseholdId, 'alpha');
    final selector = find.byKey(const Key('connect-household-selector'));
    await tester.tap(selector);
    await tester.pumpAndSettle();
    await tester.tap(find.text('beta · member').last);
    await tester.pumpAndSettle();

    expect(controller.selectedHouseholdId, 'beta');
    expect(find.text('Current role: member'), findsOneWidget);
    expect(find.byKey(const Key('connect-issue-invite')), findsNothing);
    expect(
      find.byKey(const Key('connect-issue-household-recovery')),
      findsNothing,
    );
    expect(
      find.byKey(const Key('connect-issue-device-recovery')),
      findsOneWidget,
    );
  });

  testWidgets(
    'owner/admin role gates and invite issue supports copy and expiry',
    (tester) async {
      var activeRole = 'owner';
      final requests = <http.Request>[];
      var copiedText = '';
      tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
        SystemChannels.platform,
        (call) async {
          if (call.method == 'Clipboard.setData') {
            copiedText =
                (call.arguments as Map<dynamic, dynamic>)['text'] as String;
          }
          return null;
        },
      );
      addTearDown(
        () => tester.binding.defaultBinaryMessenger.setMockMethodCallHandler(
          SystemChannels.platform,
          null,
        ),
      );

      Future<IdentityController> controllerFor(String role) async {
        activeRole = role;
        return createController(
          respond: (request) async {
            requests.add(request);
            if (request.url.path == '/households' && request.method == 'GET') {
              return http.Response(
                jsonEncode({
                  'households': [
                    {'household_id': 'family', 'role': activeRole},
                  ],
                }),
                200,
              );
            }
            if (request.url.path == '/households/invites') {
              return http.Response(
                '{"invite_code":"invite-once","expires_at":"2030-01-02T03:04:05Z"}',
                200,
              );
            }
            if (request.url.path == '/identity/device-recovery-codes') {
              return http.Response('{"recovery_code":"device-once"}', 200);
            }
            if (request.url.path == '/households/recovery-codes') {
              return http.Response('{"recovery_code":"household-once"}', 200);
            }
            return http.Response('{}', 200);
          },
        );
      }

      final owner = await controllerFor('owner');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdOnboarding(controller: owner)),
      );
      expect(find.byKey(const Key('connect-issue-invite')), findsOneWidget);
      expect(
        find.byKey(const Key('connect-issue-household-recovery')),
        findsOneWidget,
      );
      await _reveal(tester, find.byKey(const Key('connect-issue-invite')));
      await tester.tap(find.byKey(const Key('connect-issue-invite')));
      await tester.pumpAndSettle();
      expect(find.text('invite-once'), findsOneWidget);
      expect(find.textContaining('Expires:'), findsOneWidget);
      await tester.tap(find.byKey(const Key('copy-issued-code')));
      await tester.pumpAndSettle();
      expect(copiedText, 'invite-once');
      expect(find.text('Copied'), findsOneWidget);
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      expect(
        requests.any(
          (request) =>
              request.url.path == '/households/invites' &&
              request.headers['x-household-id'] == 'family',
        ),
        isTrue,
      );
      await _reveal(
        tester,
        find.byKey(const Key('connect-issue-household-recovery')),
      );
      await tester.tap(
        find.byKey(const Key('connect-issue-household-recovery')),
      );
      await tester.pumpAndSettle();
      expect(find.text('household-once'), findsOneWidget);
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();

      await _reveal(
        tester,
        find.byKey(const Key('connect-issue-device-recovery')),
      );
      await tester.tap(find.byKey(const Key('connect-issue-device-recovery')));
      await tester.pumpAndSettle();
      expect(find.text('device-once'), findsOneWidget);
      expect(find.textContaining('revokes the old install'), findsOneWidget);
      expect(
        find.textContaining('does not migrate household data'),
        findsAtLeastNWidgets(1),
      );
      await tester.tap(find.text('Done'));
      await tester.pumpAndSettle();
      final householdRecoveryIssue = requests.singleWhere(
        (request) => request.url.path == '/households/recovery-codes',
      );
      expect(householdRecoveryIssue.headers['x-household-id'], 'family');
      final deviceRecoveryIssue = requests.singleWhere(
        (request) => request.url.path == '/identity/device-recovery-codes',
      );
      expect(
        deviceRecoveryIssue.headers['authorization'],
        'Bearer install-token',
      );
      expect(
        deviceRecoveryIssue.headers.containsKey('x-household-id'),
        isFalse,
      );

      final admin = await controllerFor('admin');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdOnboarding(controller: admin)),
      );
      expect(find.byKey(const Key('connect-issue-invite')), findsOneWidget);
      expect(
        find.byKey(const Key('connect-issue-household-recovery')),
        findsNothing,
      );

      final member = await controllerFor('member');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdOnboarding(controller: member)),
      );
      expect(find.byKey(const Key('connect-issue-invite')), findsNothing);
      expect(
        find.byKey(const Key('connect-issue-household-recovery')),
        findsNothing,
      );
      expect(
        find.byKey(const Key('connect-issue-device-recovery')),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'Connect separates invite and recovery redemption from issuance',
    (tester) async {
      final requests = <http.Request>[];
      final households = <Map<String, String>>[
        {'household_id': 'home', 'role': 'owner'},
      ];
      final controller = await createController(
        respond: (request) async {
          requests.add(request);
          if (request.url.path == '/households' && request.method == 'GET') {
            return http.Response(jsonEncode({'households': households}), 200);
          }
          if (request.url.path == '/households/invites/redeem') {
            households.add({'household_id': 'joined', 'role': 'member'});
            return http.Response('{"household_id":"joined"}', 200);
          }
          if (request.url.path == '/households/recovery/redeem') {
            households.add({'household_id': 'recovered', 'role': 'owner'});
            return http.Response('{"household_id":"recovered"}', 200);
          }
          return http.Response('{}', 200);
        },
      );
      await tester.pumpWidget(
        MaterialApp(home: HouseholdOnboarding(controller: controller)),
      );

      expect(find.byKey(const Key('connect-redeem-invite')), findsOneWidget);
      expect(
        find.byKey(const Key('connect-redeem-household-recovery')),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('connect-redeem-device-recovery')),
        findsOneWidget,
      );
      expect(
        find.textContaining('does not migrate household data'),
        findsOneWidget,
      );

      await _reveal(tester, find.byKey(const Key('connect-redeem-invite')));
      await tester.tap(find.byKey(const Key('connect-redeem-invite')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, 'invite-code');
      await tester.tap(find.text('Redeem'));
      await tester.pumpAndSettle();
      expect(controller.selectedHouseholdId, 'joined');

      await _reveal(
        tester,
        find.byKey(const Key('connect-redeem-household-recovery')),
      );
      await tester.tap(
        find.byKey(const Key('connect-redeem-household-recovery')),
      );
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, 'household-recovery');
      await tester.tap(find.text('Redeem'));
      await tester.pumpAndSettle();
      expect(controller.selectedHouseholdId, 'recovered');

      await _reveal(
        tester,
        find.byKey(const Key('connect-redeem-device-recovery')),
      );
      await tester.tap(find.byKey(const Key('connect-redeem-device-recovery')));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField).last, 'device-recovery');
      await tester.tap(find.text('Redeem'));
      await tester.pumpAndSettle();
      expect(
        requests
            .map((request) => request.url.path)
            .where((path) => path.contains('/redeem')),
        [
          '/households/invites/redeem',
          '/households/recovery/redeem',
          '/identity/device-recovery/redeem',
        ],
      );
    },
  );
}
