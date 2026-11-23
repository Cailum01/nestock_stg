import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/onboarding/data/identity_api.dart';
import 'package:nestock/features/onboarding/data/google_identity_provider.dart';
import 'package:nestock/features/onboarding/data/secure_credential_store.dart';
import 'package:nestock/features/onboarding/domain/identity_controller.dart';
import 'package:nestock/features/onboarding/domain/identity_models.dart';
import 'package:nestock/features/onboarding/presentation/identity_gate.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class _MemoryCredentialStore implements SecureCredentialStore {
  String? value;
  final writes = <String>[];

  @override
  Future<String?> readBearerCredential() async => value;

  @override
  Future<void> writeBearerCredential(String credential) async {
    writes.add(credential);
    value = credential;
  }

  @override
  Future<void> deleteBearerCredential() async => value = null;
}

class _FakeGoogleIdentityProvider implements GoogleIdentityProvider {
  _FakeGoogleIdentityProvider({required this.token, this.cancelFirst = 0});

  final String? token;
  int cancelFirst;
  int signInAttempts = 0;

  @override
  bool get isConfigured => true;

  @override
  Future<String> signIn() async {
    signInAttempts++;
    if (cancelFirst > 0) {
      cancelFirst--;
      throw const GoogleSignInCancelled();
    }
    return token ?? 'interactive-google-id-token';
  }
}

void main() {
  final base = Uri.parse('https://api.example.test');

  testWidgets(
    'fresh startup launches Google and lands directly on the default household',
    (tester) async {
      final requests = <http.Request>[];
      final provider = _FakeGoogleIdentityProvider(token: 'google-id-token');
      final store = _MemoryCredentialStore();
      final controller = IdentityController(
        api: IdentityApi(
          baseUrl: base,
          client: MockClient((request) async {
            requests.add(request);
            if (request.url.path == '/identity/google') {
              expect(request.method, 'POST');
              expect(request.headers['authorization'], isNull);
              expect(jsonDecode(request.body), {
                'google_id_token': 'google-id-token',
              });
              return http.Response(
                '{"device_id":"device-1","bearer_credential":"nestock-bearer"}',
                200,
              );
            }
            if (request.url.path == '/households') {
              expect(request.method, 'GET');
              expect(request.headers['authorization'], 'Bearer nestock-bearer');
              return http.Response(
                '{"households":[{"household_id":"same-household","role":"owner"}]}',
                200,
              );
            }
            fail(
              'Unexpected identity request: ${request.method} ${request.url.path}',
            );
          }),
        ),
        credentialStore: store,
        googleIdentityProvider: provider,
      );
      await tester.pumpWidget(
        MaterialApp(
          home: IdentityGate(
            controller: controller,
            authenticatedHomeBuilder: (_, _) => const Text('cloud home'),
          ),
        ),
      );
      await tester.pumpAndSettle();

      expect(provider.signInAttempts, 1);
      expect(store.value, 'nestock-bearer');
      expect(controller.selectedHouseholdId, 'same-household');
      expect(find.text('cloud home'), findsOneWidget);
      expect(find.text('連結家庭'), findsNothing);
      expect(
        requests.map((request) => '${request.method} ${request.url.path}'),
        ['POST /identity/google', 'GET /households'],
      );
    },
  );

  testWidgets('cancelled Google selection stops and offers a retry', (
    tester,
  ) async {
    final requests = <http.Request>[];
    final provider = _FakeGoogleIdentityProvider(
      token: 'google-id-token',
      cancelFirst: 1,
    );
    final store = _MemoryCredentialStore();
    final controller = IdentityController(
      api: IdentityApi(
        baseUrl: base,
        client: MockClient((request) async {
          requests.add(request);
          if (request.url.path == '/identity/google') {
            return http.Response(
              '{"device_id":"device-1","bearer_credential":"install-bearer"}',
              200,
            );
          }
          if (request.url.path == '/households') {
            return http.Response(
              '{"households":[{"household_id":"default-household","role":"owner"}]}',
              200,
            );
          }
          fail('Unexpected identity request: ${request.url.path}');
        }),
      ),
      credentialStore: store,
      googleIdentityProvider: provider,
    );
    await tester.pumpWidget(
      MaterialApp(
        home: IdentityGate(
          controller: controller,
          authenticatedHomeBuilder: (_, _) => const Text('cloud home'),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(provider.signInAttempts, 1);
    expect(requests, isEmpty);
    expect(store.value, isNull);
    expect(
      find.text('已取消 Google 登入，請點選重試。'),
      findsOneWidget,
    );
    expect(find.text('使用 Google 重試'), findsOneWidget);

    await tester.tap(find.text('使用 Google 重試'));
    await tester.pumpAndSettle();

    expect(provider.signInAttempts, 2);
    expect(store.value, 'install-bearer');
    expect(find.text('cloud home'), findsOneWidget);
    expect(requests.map((request) => '${request.method} ${request.url.path}'), [
      'POST /identity/google',
      'GET /households',
    ]);
  });

  testWidgets('test start page opens Connect without launching Google', (
    tester,
  ) async {
    final provider = _FakeGoogleIdentityProvider(token: 'unused');
    final controller = IdentityController(
      api: IdentityApi(
        baseUrl: base,
        client: MockClient((_) async => http.Response('{}', 200)),
      ),
      credentialStore: _MemoryCredentialStore(),
      googleIdentityProvider: provider,
    );
    await tester.pumpWidget(
      MaterialApp(
        home: IdentityGate(
          controller: controller,
          connectHouseholdOnStart: true,
          authenticatedHomeBuilder: (_, _) => const Text('cloud home'),
        ),
      ),
    );
    await tester.pumpAndSettle();

    expect(provider.signInAttempts, 0);
    expect(find.text('連結家庭'), findsOneWidget);
    expect(find.byKey(const Key('connect-google-sign-in')), findsOneWidget);
    expect(find.byKey(const Key('onboarding-mode-create')), findsOneWidget);
    expect(find.byKey(const Key('onboarding-mode-join')), findsOneWidget);
    expect(
      find.byKey(const Key('onboarding-mode-householdRecovery')),
      findsNothing,
    );
    expect(
      find.byKey(const Key('onboarding-mode-deviceRecovery')),
      findsNothing,
    );
  });

  testWidgets('home brand opens Connect without changing active household', (
    tester,
  ) async {
    final controller = IdentityController(
      api: IdentityApi(
        baseUrl: base,
        client: MockClient((request) async {
          expect(request.method, 'GET');
          expect(request.url.path, '/households');
          return http.Response(
            '{"households":[{"household_id":"active-household","role":"owner"}]}',
            200,
          );
        }),
      ),
      credentialStore: _MemoryCredentialStore()..value = 'saved-bearer',
    );
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(),
        catalog: const DemoCatalog(),
        identityController: controller,
      ),
    );
    await tester.pumpAndSettle();

    expect(controller.selectedHouseholdId, 'active-household');
    final connect = find.byKey(const Key('home-brand-connect'));
    expect(connect, findsOneWidget);
    await tester.tap(connect);
    await tester.pumpAndSettle();

    expect(find.text('連結家庭'), findsOneWidget);
    expect(find.byKey(const Key('connect-household-page')), findsOneWidget);
    expect(find.byKey(const Key('connect-create-household')), findsOneWidget);
    expect(find.byKey(const Key('connect-redeem-invite')), findsOneWidget);
    expect(controller.selectedHouseholdId, 'active-household');

    await tester.pageBack();
    await tester.pumpAndSettle();
    expect(find.text('Nestock｜窩有'), findsOneWidget);
    expect(controller.selectedHouseholdId, 'active-household');
  });

  testWidgets(
    'connect-household test define opens Connect without auto sign-in',
    (tester) async {
      final provider = _FakeGoogleIdentityProvider(token: 'unused');
      final requests = <http.Request>[];
      final controller = IdentityController(
        api: IdentityApi(
          baseUrl: base,
          client: MockClient((request) async {
            requests.add(request);
            return http.Response('{}', 200);
          }),
        ),
        credentialStore: _MemoryCredentialStore(),
        googleIdentityProvider: provider,
      );
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
          identityController: controller,
          startPage: 'connect_household',
        ),
      );
      await tester.pumpAndSettle();

      expect(find.text('連結家庭'), findsOneWidget);
      expect(find.byKey(const Key('connect-google-sign-in')), findsOneWidget);
      expect(provider.signInAttempts, 0);
      expect(requests, isEmpty);
    },
  );

  test('creates, joins, switches and leaves independent households', () async {
    final active = <String, String>{};
    var created = 0;
    final requests = <http.Request>[];
    final client = MockClient((request) async {
      requests.add(request);
      final path = request.url.path;
      if (path == '/identity/bootstrap') {
        expect(request.headers['authorization'], isNull);
        expect(jsonDecode(request.body), {
          'bootstrap_challenge': 'operator-issued-one-time-value',
        });
        return http.Response(
          '{"device_id":"device-1",'
          '"bearer_credential":"install-secret-once"}',
          200,
        );
      }
      expect(request.headers['authorization'], 'Bearer install-secret-once');
      if (path == '/households' && request.method == 'POST') {
        expect(request.headers['x-household-id'], isNull);
        created++;
        final id = 'household-$created';
        active[id] = 'owner';
        return http.Response('{"household_id":"$id","role":"owner"}', 200);
      }
      if (path == '/households' && request.method == 'GET') {
        return http.Response(
          jsonEncode({
            'households': active.entries
                .map(
                  (entry) => {'household_id': entry.key, 'role': entry.value},
                )
                .toList(),
          }),
          200,
        );
      }
      if (path == '/households/invites/redeem') {
        expect(request.headers['x-household-id'], isNull);
        expect(jsonDecode(request.body), {'invite_code': 'one-time-invite'});
        active['shared-household'] = 'member';
        return http.Response(
          '{"household_id":"shared-household","role":"member"}',
          200,
        );
      }
      if (path == '/households/leave') {
        final selected = request.headers['x-household-id']!;
        expect(jsonDecode(request.body), <String, dynamic>{});
        active.remove(selected);
        return http.Response('{"household_id":"$selected","left":true}', 200);
      }
      fail('Unexpected identity request: ${request.method} $path');
    });
    final store = _MemoryCredentialStore();
    final controller = IdentityController(
      api: IdentityApi(baseUrl: base, client: client),
      credentialStore: store,
    );

    await controller.createWithChallenge('operator-issued-one-time-value');
    expect(store.value, 'install-secret-once');
    expect(store.writes, ['install-secret-once']);
    expect(store.writes, isNot(contains('operator-issued-one-time-value')));
    expect(controller.selectedHouseholdId, 'household-1');

    await controller.createHousehold();
    expect(controller.households.map((household) => household.householdId), [
      'household-1',
      'household-2',
    ]);
    await controller.joinHousehold('one-time-invite');
    expect(controller.households.map((household) => household.householdId), [
      'household-1',
      'household-2',
      'shared-household',
    ]);

    controller.selectHousehold('household-1');
    expect(controller.snapshot().headers['X-Household-Id'], 'household-1');
    controller.selectHousehold('household-2');
    expect(controller.snapshot().headers['X-Household-Id'], 'household-2');
    await controller.leaveSelectedHousehold();

    expect(controller.households.map((household) => household.householdId), [
      'household-1',
      'shared-household',
    ]);
    expect(controller.selectedHouseholdId, 'household-1');
    final leaveRequest = requests.singleWhere(
      (request) => request.url.path == '/households/leave',
    );
    expect(leaveRequest.headers['x-household-id'], 'household-2');
  });

  test('identity actions use bearer and household-scoped headers', () async {
    final requests = <http.Request>[];
    final client = MockClient((request) async {
      requests.add(request);
      expect(request.headers['authorization'], 'Bearer install-secret');
      if (request.url.path == '/households' && request.method == 'GET') {
        expect(request.headers['x-household-id'], isNull);
        return http.Response(
          '{"households":[{"household_id":"household-1","role":"owner"}]}',
          200,
        );
      }
      if (request.url.path == '/households/invites') {
        expect(request.headers['x-household-id'], 'household-1');
        return http.Response('{"invite_code":"invite-once"}', 200);
      }
      if (request.url.path == '/households/memberships/revoke') {
        expect(request.headers['x-household-id'], 'household-1');
        expect(jsonDecode(request.body), {'device_id': 'device-2'});
        return http.Response('{}', 200);
      }
      if (request.url.path == '/households/recovery-codes') {
        expect(request.headers['x-household-id'], 'household-1');
        return http.Response(
          '{"recovery_code":"household-recovery-once"}',
          200,
        );
      }
      if (request.url.path == '/identity/device-recovery-codes') {
        expect(request.headers['x-household-id'], isNull);
        return http.Response('{"recovery_code":"device-recovery-once"}', 200);
      }
      fail(
        'Unexpected identity request: ${request.method} ${request.url.path}',
      );
    });
    final controller = IdentityController(
      api: IdentityApi(baseUrl: base, client: client),
      credentialStore: _MemoryCredentialStore()..value = 'install-secret',
    );

    await controller.initialize();
    expect((await controller.createInvite()).value, 'invite-once');
    await controller.revokeMembership('device-2');
    expect(
      (await controller.issueHouseholdRecoveryCode()).value,
      'household-recovery-once',
    );
    expect(
      (await controller.issueDeviceRecoveryCode()).value,
      'device-recovery-once',
    );

    expect(requests.map((request) => request.url.path), [
      '/households',
      '/households/invites',
      '/households/memberships/revoke',
      '/households/recovery-codes',
      '/identity/device-recovery-codes',
    ]);
  });

  test(
    'expired and replayed one-time credentials fail with safe states',
    () async {
      final store = _MemoryCredentialStore();
      final api = IdentityApi(
        baseUrl: base,
        client: MockClient((request) async {
          if (request.url.path == '/identity/bootstrap') {
            return http.Response('{"detail":"sensitive backend detail"}', 401);
          }
          return http.Response('{"detail":"sensitive backend detail"}', 400);
        }),
      );

      await expectLater(
        api.bootstrap('already-used-challenge'),
        throwsA(
          isA<IdentityHttpFailure>().having(
            (failure) => failure.message,
            'message',
            contains('設定碼無效或已使用'),
          ),
        ),
      );
      await expectLater(
        api.redeemInvite('install-token', 'expired-or-replayed-invite'),
        throwsA(
          isA<IdentityHttpFailure>().having(
            (failure) => failure.message,
            'message',
            contains('邀請碼無效或已過期'),
          ),
        ),
      );
      expect(store.value, isNull);
      expect(store.writes, isEmpty);
    },
  );

  testWidgets(
    '連結頁只提供建立或加入家庭並說明設定碼保密',
    (tester) async {
      final controller = IdentityController(
        api: IdentityApi(
          baseUrl: base,
          client: MockClient((_) async => http.Response('{}', 500)),
        ),
        credentialStore: _MemoryCredentialStore(),
      );
      await tester.pumpWidget(
        MaterialApp(home: HouseholdOnboarding(controller: controller)),
      );

      expect(find.text('建立家庭'), findsNWidgets(2));
      expect(find.text('加入家庭'), findsOneWidget);
      expect(find.text('管理員提供的一次性設定碼'), findsOneWidget);
      expect(find.textContaining('App 不會儲存'), findsOneWidget);
      await tester.tap(find.byKey(const Key('onboarding-mode-join')));
      await tester.pumpAndSettle();
      expect(find.text('家庭邀請碼'), findsOneWidget);
    },
  );

  testWidgets(
    'member and owner management actions reflect their household role',
    (tester) async {
      Future<IdentityController> makeController(String role) async {
        final controller = IdentityController(
          api: IdentityApi(
            baseUrl: base,
            client: MockClient((request) async {
              if (request.url.path == '/households') {
                return http.Response(
                  jsonEncode({
                    'households': [
                      {'household_id': 'household-1', 'role': role},
                    ],
                  }),
                  200,
                );
              }
              return http.Response('{}', 200);
            }),
          ),
          credentialStore: _MemoryCredentialStore()..value = 'install-token',
        );
        await controller.initialize();
        return controller;
      }

      final member = await makeController('member');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdManagementSheet(controller: member)),
      );
      expect(find.text('Issue member invite'), findsNothing);
      expect(find.text('Revoke member device in this household'), findsNothing);
      expect(find.text('Issue household recovery code'), findsNothing);
      expect(find.text('Issue device recovery code'), findsOneWidget);

      final owner = await makeController('owner');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdManagementSheet(controller: owner)),
      );
      expect(find.text('Issue member invite'), findsOneWidget);
      expect(
        find.text('Revoke member device in this household'),
        findsOneWidget,
      );
      expect(find.text('Issue household recovery code'), findsOneWidget);

      final admin = await makeController('admin');
      await tester.pumpWidget(
        MaterialApp(home: HouseholdManagementSheet(controller: admin)),
      );
      expect(find.text('Issue member invite'), findsOneWidget);
      expect(
        find.text('Revoke member device in this household'),
        findsOneWidget,
      );
      expect(find.text('Issue household recovery code'), findsNothing);
    },
  );
}
