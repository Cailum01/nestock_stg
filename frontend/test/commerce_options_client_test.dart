import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/shopping/commerce_options_client.dart';

import 'household_test_support.dart';

void main() {
  final base = Uri.parse('https://api.example.test');

  test('HTTP commerce client URL-encodes the shopping item keyword', () async {
    final client = HttpCommerceOptionsClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient((request) async {
        expect(request.method, 'GET');
        expect(request.url.path, '/api/commerce/options');
        expect(request.url.queryParameters['keyword'], '舒潔 衛生紙');
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
        return http.Response.bytes(
          utf8.encode(
            jsonEncode({
              'status': 'success',
              'message': '查詢成功',
              'options': [
                {
                  'title': '舒潔頂級三層抽取衛生紙',
                  'store': 'momo購物網',
                  'estimated_price': 299,
                  'product_url': 'https://shop.example.test/tissue',
                  'unit_spec': '100抽 × 24包',
                },
              ],
            }),
          ),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );

    final result = await client.lookup('舒潔 衛生紙');

    expect(result.status, CommerceOptionsStatus.success);
    expect(result.options.single.title, '舒潔頂級三層抽取衛生紙');
    expect(result.options.single.estimatedPrice, 299);
  });

  test('HTTP commerce client skips malformed individual options', () async {
    final client = HttpCommerceOptionsClient(
      baseUrl: base,
      householdContext: testHouseholdContext,
      client: MockClient(
        (_) async => http.Response.bytes(
          utf8.encode(
            jsonEncode({
              'status': 'success',
              'options': [
                {'title': '有效推薦'},
                {'title': '   '},
                42,
              ],
            }),
          ),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        ),
      ),
    );

    final result = await client.lookup('洗衣精');

    expect(result.options.map((option) => option.title), ['有效推薦']);
  });

  test(
    'HTTP commerce client rejects malformed envelopes and HTTP failures',
    () async {
      final malformed = HttpCommerceOptionsClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient((_) async => http.Response('[]', 200)),
      );
      final failed = HttpCommerceOptionsClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient((_) async => http.Response('unavailable', 503)),
      );
      final missingOptions = HttpCommerceOptionsClient(
        baseUrl: base,
        householdContext: testHouseholdContext,
        client: MockClient(
          (_) async => http.Response('{"status":"success"}', 200),
        ),
      );

      await expectLater(malformed.lookup('洗衣精'), throwsFormatException);
      await expectLater(missingOptions.lookup('洗衣精'), throwsFormatException);
      await expectLater(failed.lookup('洗衣精'), throwsStateError);
    },
  );
}
