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

  testWidgets('家庭選擇只提供核准的連結操作', (tester) async {
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
    expect(find.text('連結家庭'), findsOneWidget);
    expect(find.byKey(const Key('connect-create-household')), findsOneWidget);
    expect(find.byKey(const Key('connect-redeem-invite')), findsOneWidget);
    expect(find.byKey(const Key('connect-issue-invite')), findsOneWidget);
    for (final removedAction in [
      'connect-issue-household-recovery',
      'connect-issue-device-recovery',
      'connect-redeem-household-recovery',
      'connect-redeem-device-recovery',
      'connect-revoke-member',
      'connect-leave-household',
    ]) {
      expect(find.byKey(Key(removedAction)), findsNothing);
    }

    await tester.tap(find.byKey(const Key('connect-household-selector')));
    await tester.pumpAndSettle();
    await tester.tap(find.text('beta · 成員').last);
    await tester.pumpAndSettle();

    expect(controller.selectedHouseholdId, 'beta');
    expect(find.text('目前角色：成員'), findsOneWidget);
    expect(find.byKey(const Key('connect-issue-invite')), findsNothing);
  });

  testWidgets('管理者分享系統產生的邀請碼並可複製', (tester) async {
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

    final controller = await createController(
      respond: (request) async {
        requests.add(request);
        if (request.url.path == '/households' && request.method == 'GET') {
          return http.Response(
            '{"households":[{"household_id":"family","role":"owner"}]}',
            200,
          );
        }
        if (request.url.path == '/households/invites') {
          return http.Response(
            '{"invite_code":"server-issued-invite",'
            '"expires_at":"2030-01-02T03:04:05Z"}',
            200,
          );
        }
        return http.Response('{}', 200);
      },
    );
    await tester.pumpWidget(
      MaterialApp(home: HouseholdOnboarding(controller: controller)),
    );

    await _reveal(tester, find.byKey(const Key('connect-issue-invite')));
    await tester.tap(find.byKey(const Key('connect-issue-invite')));
    await tester.pumpAndSettle();
    expect(find.text('server-issued-invite'), findsOneWidget);
    expect(find.textContaining('有效期限：'), findsOneWidget);
    expect(find.textContaining('分享給要加入的家庭成員'), findsOneWidget);

    await tester.tap(find.byKey(const Key('copy-issued-code')));
    await tester.pumpAndSettle();
    expect(copiedText, 'server-issued-invite');
    expect(find.text('已複製'), findsOneWidget);
    await tester.tap(find.text('完成'));
    await tester.pumpAndSettle();

    final inviteRequest = requests.singleWhere(
      (request) => request.url.path == '/households/invites',
    );
    expect(inviteRequest.method, 'POST');
    expect(inviteRequest.headers['authorization'], 'Bearer install-token');
    expect(inviteRequest.headers['x-household-id'], 'family');
  });

  testWidgets('新邀請碼可加入家庭且錯誤訊息不誤稱已使用', (tester) async {
    final requests = <http.Request>[];
    var listCount = 0;
    final controller = await createController(
      respond: (request) async {
        requests.add(request);
        if (request.url.path == '/households' && request.method == 'GET') {
          listCount += 1;
          final households = listCount == 1
              ? <Map<String, String>>[]
              : [
                  {'household_id': 'joined', 'role': 'member'},
                ];
          return http.Response(jsonEncode({'households': households}), 200);
        }
        if (request.url.path == '/households/invites/redeem') {
          return http.Response('{"household_id":"joined"}', 200);
        }
        return http.Response('{}', 200);
      },
    );
    await tester.pumpWidget(
      MaterialApp(home: HouseholdOnboarding(controller: controller)),
    );

    expect(find.text('尚未加入任何家庭。'), findsOneWidget);
    await _reveal(tester, find.byKey(const Key('connect-redeem-invite')));
    await tester.tap(find.byKey(const Key('connect-redeem-invite')));
    await tester.pumpAndSettle();
    await tester.enterText(
      find.byType(TextField).last,
      'fresh-invite-code-generated-by-server',
    );
    await tester.tap(find.text('確認加入'));
    await tester.pumpAndSettle();

    expect(controller.selectedHouseholdId, 'joined');
    expect(find.byKey(const Key('connect-error')), findsNothing);
    final redeem = requests.singleWhere(
      (request) => request.url.path == '/households/invites/redeem',
    );
    expect(redeem.headers['authorization'], 'Bearer install-token');
    expect(jsonDecode(redeem.body), {
      'invite_code': 'fresh-invite-code-generated-by-server',
    });
  });

  testWidgets('邀請碼錯誤提示使用繁體中文且不誤稱已使用', (tester) async {
    final controller = await createController(
      respond: (request) async {
        if (request.url.path == '/households' && request.method == 'GET') {
          return http.Response('{"households":[]}', 200);
        }
        if (request.url.path == '/households/invites/redeem') {
          return http.Response('{"detail":"Invite is invalid."}', 400);
        }
        return http.Response('{}', 200);
      },
    );
    await tester.pumpWidget(
      MaterialApp(home: HouseholdOnboarding(controller: controller)),
    );
    await _reveal(tester, find.byKey(const Key('connect-redeem-invite')));
    await tester.tap(find.byKey(const Key('connect-redeem-invite')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField).last, 'incorrect-code');
    await tester.tap(find.text('確認加入'));
    await tester.pumpAndSettle();

    expect(find.text('邀請碼無效或已過期，請確認後再試。'), findsOneWidget);
    expect(find.textContaining('已使用'), findsNothing);
  });
}
