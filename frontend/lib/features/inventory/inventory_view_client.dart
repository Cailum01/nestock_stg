import 'dart:convert';

import 'package:http/http.dart' as http;

import '../onboarding/domain/household_context.dart';
import '../onboarding/data/secure_transport_client.dart';

/// Presentation projections only; these do not define the backend wire contract.
class InventoryViewItem {
  const InventoryViewItem(
    this.name,
    this.quantity,
    this.unit, {
    this.aliases = const [],
    this.location,
    this.expiry,
  });
  final String name, quantity, unit;
  final List<String> aliases;
  final String? location;
  final DateTime? expiry;
}

abstract interface class InventoryViewClient {
  Future<List<InventoryViewItem>> load();
}

class DemoInventoryViewClient implements InventoryViewClient {
  const DemoInventoryViewClient();
  @override
  Future<List<InventoryViewItem>> load() async => [
    InventoryViewItem('白米', '2', '包', location: '廚房櫃子'),
    InventoryViewItem(
      '咖啡',
      '2',
      '包',
      location: '廚房',
      expiry: DateTime(2026, 11, 13),
    ),
    InventoryViewItem(
      '衛生紙',
      '3',
      '包',
      location: '儲藏櫃',
      aliases: ['廁所紙', 'toilet paper'],
    ),
    InventoryViewItem('牛奶', '1', '瓶', expiry: DateTime(2026, 12, 1)),
    const InventoryViewItem('肥皂', '2', '個'),
  ];
}

class HttpInventoryViewClient implements InventoryViewClient {
  HttpInventoryViewClient({
    required this.baseUrl,
    required this.householdContext,
    http.Client? client,
    this.timeout = const Duration(seconds: 15),
  }) : client = SecureTransportClient(innerClient: client);

  final Uri baseUrl;
  final HouseholdRequestContext householdContext;
  final http.Client client;
  final Duration timeout;

  @override
  Future<List<InventoryViewItem>> load() async {
    final scope = householdContext.snapshot();
    final response = await client
        .get(baseUrl.resolve('/inventory'), headers: scope.headers)
        .timeout(timeout);
    if (response.statusCode != 200) {
      throw StateError('Inventory request failed (${response.statusCode})');
    }

    final body = jsonDecode(response.body);
    if (body is! List) {
      throw const FormatException('Inventory response must be a list');
    }
    return body
        .whereType<Map>()
        .map((record) => _viewItemFromJson(Map<String, dynamic>.from(record)))
        .whereType<InventoryViewItem>()
        .toList(growable: false);
  }

  InventoryViewItem? _viewItemFromJson(Map<String, dynamic> record) {
    final name = record['name'];
    final quantity = record['quantity'];
    final unit = record['unit'];
    if (name is! String ||
        name.trim().isEmpty ||
        quantity is! num ||
        unit is! String ||
        unit.trim().isEmpty) {
      return null;
    }

    final aliases = record['aliases'];
    final location = record['location'];
    final expiry = record['expiry_date'];
    return InventoryViewItem(
      name.trim(),
      quantity.toString(),
      unit.trim(),
      aliases: aliases is List
          ? aliases
                .whereType<String>()
                .map((alias) => alias.trim())
                .where((alias) => alias.isNotEmpty)
                .toList(growable: false)
          : const [],
      location: location is String && location.trim().isNotEmpty
          ? location.trim()
          : null,
      expiry: expiry is String ? DateTime.tryParse(expiry) : null,
    );
  }
}

InventoryViewClient inventoryViewClientForTransport({
  required String transport,
  required Uri baseUrl,
  HouseholdRequestContext? householdContext,
}) => transport == 'mock'
    ? const DemoInventoryViewClient()
    : HttpInventoryViewClient(
        baseUrl: baseUrl,
        householdContext:
            householdContext ??
            (throw ArgumentError('HTTP inventory requires household context.')),
      );
