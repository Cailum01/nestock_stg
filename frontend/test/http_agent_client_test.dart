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
  test('server errors and unsupported approvals fail safely', () async {
    final client = HttpAgentClient(
      baseUrl: base,
      client: MockClient((_) async => http.Response('', 501)),
    );
    await expectLater(client.send('查詢'), throwsStateError);
    final approval = HttpAgentClient(
      baseUrl: base,
      client: MockClient(
        (_) async =>
            http.Response('{"message":"x","pending_approval":{}}', 200),
      ),
    );
    await expectLater(approval.send('查詢'), throwsUnsupportedError);
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
