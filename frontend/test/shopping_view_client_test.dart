import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';

import 'household_test_support.dart';

void main() {
  final base = Uri.parse('http://10.0.2.2:8000');

  test('HTTP Shopping client maps populated shopping records', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        expect(request.method, 'GET');
        expect(request.url.path, '/shopping');
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
        return http.Response.bytes(
          utf8.encode(
            jsonEncode([
              {'name': '牛奶', 'quantity': 2, 'unit': '瓶', 'status': 'pending'},
            ]),
          ),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    final items = await client.load();

    expect(items, hasLength(1));
    expect(items.single.name, '牛奶');
    expect(items.single.quantity, 2);
    expect(items.single.unit, '瓶');
    expect(items.single.status, 'pending');
  });

  test('HTTP Shopping client maps an empty response', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((_) async => http.Response('[]', 200)),
    );

    expect(await client.load(), isEmpty);
  });

  test(
    'HTTP Shopping client preserves missing intent quantity and unit',
    () async {
      final client = HttpShoppingViewClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient(
          (_) async => http.Response.bytes(
            utf8.encode(
              jsonEncode([
                {
                  'name': '豆漿',
                  'quantity': null,
                  'unit': null,
                  'status': 'pending',
                },
                {'name': '雞蛋', 'status': 'pending'},
              ]),
            ),
            200,
            headers: {'content-type': 'application/json; charset=utf-8'},
          ),
        ),
      );

      final items = await client.load();

      expect(items.map((item) => item.name), ['豆漿', '雞蛋']);
      expect(items.every((item) => item.quantity == null), isTrue);
      expect(items.every((item) => item.unit == null), isTrue);
    },
  );

  test('HTTP Shopping client skips malformed records safely', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient(
        (_) async => http.Response.bytes(
          utf8.encode(
            jsonEncode([
              {'name': '有效項目', 'status': 'pending'},
              42,
              {'name': '', 'status': 'pending'},
            ]),
          ),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        ),
      ),
    );

    final items = await client.load();

    expect(items.map((item) => item.name), ['有效項目']);
  });

  test('HTTP failures do not return demo Shopping data', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((_) async => http.Response('unavailable', 503)),
    );

    await expectLater(client.load(), throwsStateError);
  });

  test('Shopping client transport wiring selects HTTP or demo clients', () {
    expect(
      shoppingViewClientForTransport(
        transport: 'http',
        baseUrl: base,
        householdContext: testHouseholdContext,
      ),
      isA<HttpShoppingViewClient>(),
    );
    expect(
      shoppingViewClientForTransport(transport: 'mock', baseUrl: base),
      isA<DemoShoppingViewClient>(),
    );
  });
}
