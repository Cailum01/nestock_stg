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

  test('HTTP Shopping add sends manual intent in authorized household scope', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        expect(request.method, 'POST');
        expect(request.url.path, '/shopping');
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
        expect(request.headers['content-type'], contains('application/json'));
        final payload = jsonDecode(request.body) as Map<String, dynamic>;
        expect(payload.keys.toSet(), {
          'request_id',
          'name',
          'quantity',
          'unit',
        });
        expect(payload['request_id'], 'request-1');
        expect(payload['name'], '  豆漿  ');
        expect(payload['quantity'], 2);
        expect(payload['unit'], '瓶');
        expect(payload.containsKey('price'), isFalse);
        return http.Response.bytes(
          utf8.encode(
            jsonEncode({
              'name': '豆漿',
              'quantity': 2,
              'unit': '瓶',
              'status': 'pending',
            }),
          ),
          201,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    final item = await client.add(
      requestId: 'request-1',
      name: '  豆漿  ',
      quantity: 2,
      unit: '瓶',
    );

    expect(item.name, '豆漿');
    expect(item.quantity, 2);
    expect(item.unit, '瓶');
    expect(item.status, 'pending');
  });

  test('HTTP Shopping add omits unspecified quantity and unit', () async {
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        final payload = jsonDecode(request.body) as Map<String, dynamic>;
        expect(payload, {'request_id': 'request-2', 'name': '雞蛋'});
        return http.Response.bytes(
          utf8.encode(jsonEncode({'name': '雞蛋', 'status': 'pending'})),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    await client.add(requestId: 'request-2', name: '雞蛋');
  });

  test('HTTP Shopping add maps authorization and request conflict responses', () async {
    for (final (status, failure) in [
      (401, ShoppingAddFailure.unauthenticated),
      (403, ShoppingAddFailure.forbidden),
      (409, ShoppingAddFailure.requestConflict),
      (503, ShoppingAddFailure.unavailable),
      (422, ShoppingAddFailure.validation),
    ]) {
      final client = HttpShoppingViewClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient((_) async => http.Response('error', status)),
      );
      await expectLater(
        client.add(requestId: 'request-1', name: '牛奶'),
        throwsA(
          isA<ShoppingAddException>().having(
            (error) => error.failure,
            'failure',
            failure,
          ),
        ),
      );
    }
  });

  test('successful HTTP Shopping add appears in a subsequent list read', () async {
    final savedRecords = <Map<String, dynamic>>[];
    final client = HttpShoppingViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
        if (request.method == 'POST') {
          final payload = jsonDecode(request.body) as Map<String, dynamic>;
          savedRecords.add({
            'name': payload['name'],
            'quantity': payload['quantity'],
            'unit': payload['unit'],
            'status': 'pending',
          });
          return http.Response(
            jsonEncode(savedRecords.last),
            201,
            headers: {'content-type': 'application/json; charset=utf-8'},
          );
        }
        expect(request.method, 'GET');
        return http.Response(
          jsonEncode(savedRecords),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    await client.add(
      requestId: 'request-round-trip',
      name: '豆漿',
      quantity: 2,
      unit: '瓶',
    );
    final items = await client.load();

    expect(items, hasLength(1));
    expect(items.single.name, '豆漿');
    expect(items.single.quantity, 2);
    expect(items.single.unit, '瓶');
    expect(items.single.status, 'pending');
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
