import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/agent/data/http_agent_client.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';

void main() {
  final base = Uri.parse('http://10.0.2.2:8000');
  test(
    'maps message and structured inventory and sends opaque reference',
    () async {
      final client = HttpAgentClient(
        baseUrl: base,
        client: MockClient((request) async {
          expect(request.url.path, '/agent/messages');
          expect(request.method, 'POST');
          expect(jsonDecode(request.body), {
            'household_id': 'demo-household',
            'message': '查詢',
            'image_ref': 'opaque:Ref',
          });
          return http.Response(
            jsonEncode({
              'message': '結果',
              'results': [
                {
                  'kind': 'inventory',
                  'summary': '庫存',
                  'data': {
                    'items': [
                      {'name': '白米', 'quantity': 2, 'unit': '包'},
                    ],
                  },
                },
              ],
              'pending_approval': null,
            }),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );
      final reply = await client.sendWithPhoto('查詢', 'opaque:Ref');
      expect(reply.message, '結果');
      expect(reply.results.single.kind, ResultKind.inventory);
      expect(reply.results.single.data['items'][0]['name'], '白米');
    },
  );
  test('server errors fail safely', () async {
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((_) async => http.Response('', 501)),
    );
    await expectLater(client.send('查詢'), throwsStateError);
  });
  test(
    'parses pending item-match cards and sends selected option IDs',
    () async {
      final client = HttpAgentClient(
        baseUrl: base,
        client: MockClient((request) async {
          if (request.url.path == '/agent/messages') {
            return http.Response.bytes(
              utf8.encode(
                jsonEncode({
                  'message': '請確認品項',
                  'results': [],
                  'pending_approval': {
                    'approval_id': 'approval-match-1',
                    'presentation': 'item_match',
                    'operations': [
                      {
                        'operation_id': 'operation-match-1',
                        'action': 'confirm_item_match',
                        'description': '這是家裡的洗潔精嗎？',
                        'values': {
                          'user_term': '洗碗精',
                          'candidate_item_id': 'item-cleanser',
                          'candidate_name': '洗潔精',
                          'quantity': 1,
                          'unit': '瓶',
                        },
                      },
                    ],
                    'options': [
                      {'option_id': 'same_item', 'label': '是，加到洗潔精'},
                      {'option_id': 'new_item', 'label': '不是，這是新的東西'},
                    ],
                  },
                }),
              ),
              200,
              headers: {'content-type': 'application/json; charset=utf-8'},
            );
          }
          expect(request.url.path, '/agent/resume');
          expect(jsonDecode(request.body), {
            'household_id': 'demo-household',
            'approval_id': 'approval-match-1',
            'approved': true,
            'selected_option_id': 'new_item',
          });
          return http.Response.bytes(
            utf8.encode(jsonEncode({'message': '新品項確認', 'results': []})),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );

      final reply = await client.send('洗碗精買回來了');

      expect(reply.approval?.id, 'approval-match-1');
      expect(reply.approval?.presentation, ApprovalPresentation.itemMatch);
      expect(reply.approval?.operations.single.action, 'confirm_item_match');
      expect(reply.approval?.operations.single.values['candidate_name'], '洗潔精');
      expect(reply.approval?.options.map((option) => option.id), [
        'same_item',
        'new_item',
      ]);
      expect(
        (await client.resume(
          approvalId: 'approval-match-1',
          approved: true,
          selectedOptionId: 'new_item',
        )).message,
        '新品項確認',
      );
    },
  );
  test('only edited approval messages include replaces_approval_id', () async {
    final requests = <Map<String, dynamic>>[];
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((request) async {
        requests.add(
          Map<String, dynamic>.from(jsonDecode(request.body) as Map),
        );
        return http.Response.bytes(
          utf8.encode(jsonEncode({'message': '收到', 'results': []})),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    await client.send('一般聊天');
    await client.send('衛生紙：數量改成 3', replacesApprovalId: 'approval-1');

    expect(requests, [
      {'household_id': 'demo-household', 'message': '一般聊天'},
      {
        'household_id': 'demo-household',
        'message': '衛生紙：數量改成 3',
        'replaces_approval_id': 'approval-1',
      },
    ]);
  });
  test('rejecting an approval omits selection fields', () async {
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((request) async {
        expect(request.url.path, '/agent/resume');
        expect(jsonDecode(request.body), {
          'household_id': 'demo-household',
          'approval_id': 'approval-1',
          'approved': false,
        });
        return http.Response.bytes(
          utf8.encode(jsonEncode({'message': '已取消', 'results': []})),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    expect(
      (await client.resume(approvalId: 'approval-1', approved: false)).message,
      '已取消',
    );
  });
  test(
    'parses unlabeled purchase options and sends selected operation IDs',
    () async {
      final client = HttpAgentClient(
        baseUrl: base,
        client: MockClient((request) async {
          if (request.url.path == '/agent/messages') {
            return http.Response.bytes(
              utf8.encode(
                jsonEncode({
                  'message': '請選購買選項',
                  'results': [],
                  'pending_approval': {
                    'approval_id': 'approval-options-1',
                    'presentation': 'purchase_options',
                    'operations': [
                      {
                        'operation_id': 'operation-options-1',
                        'action': 'choose_purchase_option',
                        'description': '選購項目',
                        'values': {},
                      },
                    ],
                    'options': [
                      {'option_id': 'option-1', 'store': '便利商店', 'price': 109},
                    ],
                  },
                }),
              ),
              200,
              headers: {'content-type': 'application/json; charset=utf-8'},
            );
          }
          expect(jsonDecode(request.body), {
            'household_id': 'demo-household',
            'approval_id': 'approval-options-1',
            'approved': true,
            'selected_option_id': 'option-1',
          });
          return http.Response.bytes(
            utf8.encode(jsonEncode({'message': '完成', 'results': []})),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }),
      );

      final reply = await client.send('幫我買衛生紙');
      expect(reply.approval?.options.single.label, isNull);
      expect(reply.approval?.options.single.values, {
        'store': '便利商店',
        'price': 109,
      });
      await client.resume(
        approvalId: 'approval-options-1',
        approved: true,
        selectedOptionId: 'option-1',
      );
    },
  );
  test('batch resume sends exact backend operation IDs', () async {
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((request) async {
        expect(jsonDecode(request.body), {
          'household_id': 'demo-household',
          'approval_id': 'approval-batch-1',
          'approved': true,
          'approved_operation_ids': ['operation-a'],
        });
        return http.Response.bytes(
          utf8.encode(jsonEncode({'message': '完成', 'results': []})),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    await client.resume(
      approvalId: 'approval-batch-1',
      approved: true,
      approvedOperationIds: ['operation-a'],
    );
  });
  test(
    'parses complete-purchase values without inventing a location',
    () async {
      final client = HttpAgentClient(
        baseUrl: base,
        client: MockClient(
          (_) async => http.Response.bytes(
            utf8.encode(
              jsonEncode({
                'message': '請確認購買',
                'results': [],
                'pending_approval': {
                  'approval_id': 'approval-purchase-1',
                  'presentation': 'single_action',
                  'operations': [
                    {
                      'operation_id': 'operation-purchase-1',
                      'action': 'complete_purchase',
                      'description': '衛生紙 +2 包',
                      'values': {
                        'name': '衛生紙',
                        'purchased_quantity': 2,
                        'purchased_unit': '包',
                        'shopping_item_id': 'shopping-1',
                        'item_id': 'item-1',
                        'transaction_id': 'transaction-1',
                      },
                    },
                  ],
                  'options': [],
                },
              }),
            ),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          ),
        ),
      );

      final reply = await client.send('我買了衛生紙 2 包');
      final operation = reply.approval!.operations.single;
      expect(operation.action, 'complete_purchase');
      expect(operation.values['purchased_quantity'], 2);
      expect(operation.values.containsKey('location'), isFalse);
    },
  );
  test('demo-looking text is sent to the backend as ordinary text', () async {
    final messages = <String>[];
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((request) async {
        messages.add((jsonDecode(request.body) as Map)['message'] as String);
        return http.Response(
          jsonEncode({'message': 'backend reply', 'results': []}),
          200,
        );
      }),
    );

    for (final message in ['/vision-demo', '/confirmation-demo']) {
      expect((await client.send(message)).message, 'backend reply');
    }

    expect(messages, ['/vision-demo', '/confirmation-demo']);
  });
  test('request timeout is exposed and mock remains usable', () async {
    final client = HttpAgentClient(
      baseUrl: base,
      timeout: const Duration(milliseconds: 10),
      client: MockClient((_) => Completer<http.Response>().future),
    );
    await expectLater(client.send('查詢'), throwsA(isA<TimeoutException>()));
    expect(
      (await MockAgentClient(scenario: MockScenario.conversation).send('查詢'))
          .message,
      isNotEmpty,
    );
  });
}
