import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/inventory/inventory_view_client.dart';

import 'household_test_support.dart';

void main() {
  final base = Uri.parse('http://10.0.2.2:8000');

  test('HTTP Inventory client maps populated inventory records', () async {
    final client = HttpInventoryViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        expect(request.method, 'GET');
        expect(request.url.path, '/inventory');
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
        return http.Response.bytes(
          utf8.encode(
            jsonEncode([
              {
                'name': '白米',
                'quantity': 2.0,
                'unit': '包',
                'aliases': ['米', 42, '  主食  '],
                'location': ' 廚房櫃子 ',
                'expiry_date': '2026-12-01T00:00:00Z',
              },
            ]),
          ),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    final items = await client.load();

    expect(items, hasLength(1));
    expect(items.single.name, '白米');
    expect(items.single.quantity, '2.0');
    expect(items.single.unit, '包');
    expect(items.single.aliases, ['米', '主食']);
    expect(items.single.location, '廚房櫃子');
    expect(items.single.expiry, DateTime.parse('2026-12-01T00:00:00Z'));
  });

  test('HTTP Inventory client skips malformed individual records', () async {
    final client = HttpInventoryViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient(
        (_) async => http.Response.bytes(
          utf8.encode(
            jsonEncode([
              {'name': '有效項目', 'quantity': 3, 'unit': '包'},
              42,
              {'name': '', 'quantity': 1, 'unit': '個'},
              {'name': '缺少數量', 'unit': '個'},
              {'name': '缺少單位', 'quantity': 1},
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

  test('HTTP Inventory client rejects malformed top-level payloads', () async {
    final client = HttpInventoryViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient(
        (_) async => http.Response.bytes(
          utf8.encode(jsonEncode({'name': '白米'})),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        ),
      ),
    );

    await expectLater(client.load(), throwsFormatException);
  });

  test('HTTP Inventory client surfaces non-success responses', () async {
    final client = HttpInventoryViewClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((_) async => http.Response('unavailable', 503)),
    );

    await expectLater(client.load(), throwsStateError);
  });

  test(
    'HTTP Inventory client surfaces network failures without demo fallback',
    () async {
      final client = HttpInventoryViewClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient(
          (_) => throw http.ClientException('network unavailable'),
        ),
      );

      await expectLater(client.load(), throwsA(isA<http.ClientException>()));
    },
  );

  test(
    'Inventory client transport wiring selects HTTP or demo clients',
    () async {
      expect(
        inventoryViewClientForTransport(
          transport: 'http',
          baseUrl: base,
          householdContext: testHouseholdContext,
        ),
        isA<HttpInventoryViewClient>(),
      );
      expect(
        inventoryViewClientForTransport(transport: 'mock', baseUrl: base),
        isA<DemoInventoryViewClient>(),
      );

      final mockItems = await inventoryViewClientForTransport(
        transport: 'mock',
        baseUrl: base,
      ).load();
      expect(mockItems.map((item) => item.name), contains('衛生紙'));
    },
  );
}
