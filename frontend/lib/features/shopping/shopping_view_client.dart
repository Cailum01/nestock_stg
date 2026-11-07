import 'dart:convert';

import 'package:http/http.dart' as http;

/// Presentation projection only; this does not define the backend wire contract.
class ShoppingViewItem {
  const ShoppingViewItem({
    required this.name,
    required this.status,
    this.quantity,
    this.unit,
  });

  final String name;
  final String status;
  final num? quantity;
  final String? unit;
}

abstract interface class ShoppingViewClient {
  Future<List<ShoppingViewItem>> load();
}

class DemoShoppingViewClient implements ShoppingViewClient {
  const DemoShoppingViewClient();
  @override
  Future<List<ShoppingViewItem>> load() async => const [
    ShoppingViewItem(name: '牛奶', quantity: 1, unit: '瓶', status: 'pending'),
    ShoppingViewItem(name: '衛生紙', quantity: 1, unit: '包', status: 'pending'),
    ShoppingViewItem(name: '咖啡豆', quantity: 2, unit: '包', status: 'pending'),
  ];
}

class HttpShoppingViewClient implements ShoppingViewClient {
  HttpShoppingViewClient({
    required this.baseUrl,
    http.Client? client,
    this.timeout = const Duration(seconds: 15),
  }) : client = client ?? http.Client();

  final Uri baseUrl;
  final http.Client client;
  final Duration timeout;

  @override
  Future<List<ShoppingViewItem>> load() async {
    final response = await client
        .get(baseUrl.resolve('/shopping'))
        .timeout(timeout);
    if (response.statusCode != 200) {
      throw StateError('Shopping request failed (${response.statusCode})');
    }

    final body = jsonDecode(response.body);
    if (body is! List) {
      throw const FormatException('Shopping response must be a list');
    }
    return body
        .whereType<Map>()
        .map((record) => _viewItemFromJson(Map<String, dynamic>.from(record)))
        .whereType<ShoppingViewItem>()
        .toList(growable: false);
  }

  ShoppingViewItem? _viewItemFromJson(Map<String, dynamic> record) {
    final name = record['name'];
    final status = record['status'];
    if (name is! String ||
        name.trim().isEmpty ||
        status is! String ||
        status.isEmpty) {
      return null;
    }
    final unit = record['unit'];
    return ShoppingViewItem(
      name: name.trim(),
      status: status,
      quantity: record['quantity'] is num ? record['quantity'] as num : null,
      unit: unit is String && unit.trim().isNotEmpty ? unit.trim() : null,
    );
  }
}

ShoppingViewClient shoppingViewClientForTransport({
  required String transport,
  required Uri baseUrl,
}) => transport == 'mock'
    ? const DemoShoppingViewClient()
    : HttpShoppingViewClient(baseUrl: baseUrl);
