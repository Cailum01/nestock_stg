import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/agent/data/http_agent_client.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/onboarding/domain/household_context.dart';

class _MutableHouseholdContext implements HouseholdRequestContext {
  String selectedHouseholdId = 'household-original';

  @override
  HouseholdRequestScope snapshot({String? householdIdOverride}) =>
      HouseholdRequestScope(
        bearerCredential: 'install-token',
        householdId: householdIdOverride ?? selectedHouseholdId,
      );
}

void main() {
  test(
    'approval action stays bound to its original household after a switch',
    () async {
      final context = _MutableHouseholdContext();
      final client = HttpAgentClient(
        baseUrl: Uri.parse('https://api.example.test'),
        householdContext: context,
        client: MockClient((request) async {
          expect(request.headers['authorization'], 'Bearer install-token');
          expect(request.headers['x-household-id'], 'household-original');
          expect(
            (jsonDecode(request.body) as Map)['household_id'],
            'household-original',
          );
          if (request.url.path == '/agent/messages') {
            return http.Response(
              jsonEncode({
                'message': 'Confirm this action',
                'results': [],
                'pending_approval': {
                  'approval_id': 'approval-1',
                  'presentation': 'single_action',
                  'operations': [
                    {
                      'operation_id': 'operation-1',
                      'action': 'add_item',
                      'description': 'Add rice',
                      'values': {},
                    },
                  ],
                  'options': [],
                },
              }),
              200,
            );
          }
          expect(request.url.path, '/agent/resume');
          return http.Response(
            jsonEncode({'message': 'Completed', 'results': []}),
            200,
          );
        }),
      );

      final initial = await client.send('Add rice');
      expect(initial.approval?.householdId, 'household-original');
      context.selectedHouseholdId = 'household-switched';

      final reply = await client.resumeInHousehold(
        approvalId: initial.approval!.id,
        approved: true,
        householdId: initial.approval!.householdId!,
      );

      expect(reply.message, 'Completed');
      expect(
        context.snapshot().householdId,
        'household-switched',
        reason: 'The current selection remains switched after the card action.',
      );
    },
  );

  test('approval edit requests also retain the bound household', () async {
    final context = _MutableHouseholdContext();
    context.selectedHouseholdId = 'another-household';
    final client = HttpAgentClient(
      baseUrl: Uri.parse('https://api.example.test'),
      householdContext: context,
      client: MockClient((request) async {
        expect(request.url.path, '/agent/messages');
        expect(request.headers['x-household-id'], 'household-card-owner');
        expect(
          (jsonDecode(request.body) as Map)['household_id'],
          'household-card-owner',
        );
        return http.Response(
          jsonEncode({'message': 'Updated', 'results': []}),
          200,
        );
      }),
    );

    final reply = await client.submitCardChangesInHousehold(
      approvalId: 'approval-2',
      householdId: 'household-card-owner',
      changes: [CardChange(operationId: 'operation-2', quantity: 2)],
    );

    expect(reply.message, 'Updated');
  });
}
