import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:http/http.dart' as http;

import '../onboarding/domain/household_context.dart';
import '../onboarding/data/secure_transport_client.dart';

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

class AddShoppingItemRequest {
  const AddShoppingItemRequest({
    required this.requestId,
    required this.name,
    this.quantity,
    this.unit,
  });

  final String requestId;
  final String name;
  final int? quantity;
  final String? unit;

  Map<String, Object> toJson() => {
    'request_id': requestId,
    'name': name.trim(),
    'quantity': ?quantity,
    'unit': ?unit?.trim(),
  };
}

enum ShoppingAddFailure {
  validation,
  unauthenticated,
  forbidden,
  requestConflict,
  unavailable,
  timeout,
  network,
  malformedResponse,
  unexpectedResponse,
}

class ShoppingAddException implements Exception {
  const ShoppingAddException(this.failure, {this.statusCode});

  final ShoppingAddFailure failure;
  final int? statusCode;

  @override
  String toString() => 'ShoppingAddException($failure, status: $statusCode)';
}

abstract interface class ShoppingViewClient {
  Future<List<ShoppingViewItem>> load();
}

abstract interface class ShoppingManualAddClient {
  Future<ShoppingViewItem> add({
    required String requestId,
    required String name,
    int? quantity,
    String? unit,
  });
}

class DemoShoppingViewClient
    implements ShoppingViewClient, ShoppingManualAddClient {
  const DemoShoppingViewClient();
  @override
  Future<List<ShoppingViewItem>> load() async => const [
    ShoppingViewItem(name: '牛奶', quantity: 1, unit: '瓶', status: 'pending'),
    ShoppingViewItem(name: '衛生紙', quantity: 1, unit: '包', status: 'pending'),
    ShoppingViewItem(name: '咖啡豆', quantity: 2, unit: '包', status: 'pending'),
  ];

  @override
  Future<ShoppingViewItem> add({
    required String requestId,
    required String name,
    int? quantity,
    String? unit,
  }) async => throw const ShoppingAddException(ShoppingAddFailure.unavailable);
}

class HttpShoppingViewClient
    implements ShoppingViewClient, ShoppingManualAddClient {
  HttpShoppingViewClient({
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
  Future<List<ShoppingViewItem>> load() async {
    final scope = householdContext.snapshot();
    final response = await client
        .get(baseUrl.resolve('/shopping'), headers: scope.headers)
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

  @override
  Future<ShoppingViewItem> add({
    required String requestId,
    required String name,
    int? quantity,
    String? unit,
  }) async {
    final scope = householdContext.snapshot();
    late final http.Response response;
    try {
      response = await client
          .post(
            baseUrl.resolve('/shopping'),
            headers: {
              ...scope.headers,
              'content-type': 'application/json; charset=utf-8',
            },
            body: jsonEncode({
              'request_id': requestId,
              'name': name,
              'quantity': ?quantity,
              'unit': ?unit,
            }),
          )
          .timeout(timeout);
    } on TimeoutException {
      throw const ShoppingAddException(ShoppingAddFailure.timeout);
    } on http.ClientException {
      throw const ShoppingAddException(ShoppingAddFailure.network);
    }

    switch (response.statusCode) {
      case 200:
      case 201:
        try {
          final body = jsonDecode(response.body);
          if (body is! Map) {
            throw const ShoppingAddException(
              ShoppingAddFailure.malformedResponse,
            );
          }
          final item = _viewItemFromJson(Map<String, dynamic>.from(body));
          if (item == null) {
            throw const ShoppingAddException(
              ShoppingAddFailure.malformedResponse,
            );
          }
          return item;
        } on ShoppingAddException {
          rethrow;
        } on FormatException {
          throw const ShoppingAddException(
            ShoppingAddFailure.malformedResponse,
          );
        }
      case 400:
      case 422:
        throw ShoppingAddException(
          ShoppingAddFailure.validation,
          statusCode: response.statusCode,
        );
      case 401:
        throw const ShoppingAddException(
          ShoppingAddFailure.unauthenticated,
          statusCode: 401,
        );
      case 403:
        throw const ShoppingAddException(
          ShoppingAddFailure.forbidden,
          statusCode: 403,
        );
      case 409:
        throw const ShoppingAddException(
          ShoppingAddFailure.requestConflict,
          statusCode: 409,
        );
      case 503:
        throw const ShoppingAddException(
          ShoppingAddFailure.unavailable,
          statusCode: 503,
        );
      default:
        throw ShoppingAddException(
          ShoppingAddFailure.unexpectedResponse,
          statusCode: response.statusCode,
        );
    }
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

final Random _shoppingRequestRandom = Random.secure();

String newShoppingRequestId() {
  final bytes = List<int>.generate(
    16,
    (_) => _shoppingRequestRandom.nextInt(256),
  );
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  final hex = bytes
      .map((byte) => byte.toRadixString(16).padLeft(2, '0'))
      .join();
  return 'shopping-${DateTime.now().microsecondsSinceEpoch}-'
      '${hex.substring(0, 8)}-${hex.substring(8, 12)}-'
      '${hex.substring(12, 16)}-${hex.substring(16, 20)}-${hex.substring(20)}';
}

String createShoppingRequestId() => newShoppingRequestId();

ShoppingViewClient shoppingViewClientForTransport({
  required String transport,
  required Uri baseUrl,
  HouseholdRequestContext? householdContext,
}) => transport == 'mock'
    ? const DemoShoppingViewClient()
    : HttpShoppingViewClient(
        baseUrl: baseUrl,
        householdContext:
            householdContext ??
            (throw ArgumentError('HTTP shopping requires household context.')),
      );
