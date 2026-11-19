import 'dart:convert';

import 'package:http/http.dart' as http;

import '../onboarding/data/secure_transport_client.dart';
import '../onboarding/domain/household_context.dart';

enum CommerceOptionsStatus { success, noMatch, unavailable }

class CommerceOptionView {
  const CommerceOptionView({
    required this.title,
    this.store,
    this.source,
    this.estimatedPrice,
    this.productUrl,
    this.unitSpec,
    this.imageUrl,
  });

  final String title;
  final String? store;
  final String? source;
  final num? estimatedPrice;
  final String? productUrl;
  final String? unitSpec;
  final String? imageUrl;
}

class CommerceOptionsView {
  const CommerceOptionsView({
    required this.status,
    required this.message,
    this.options = const [],
  });

  final CommerceOptionsStatus status;
  final String message;
  final List<CommerceOptionView> options;
}

abstract interface class CommerceOptionsClient {
  Future<CommerceOptionsView> lookup(String keyword);
}

class UnavailableCommerceOptionsClient implements CommerceOptionsClient {
  const UnavailableCommerceOptionsClient();

  @override
  Future<CommerceOptionsView> lookup(String keyword) async =>
      const CommerceOptionsView(
        status: CommerceOptionsStatus.unavailable,
        message: '目前無法取得採購推薦，請稍後再試。',
      );
}

class HttpCommerceOptionsClient implements CommerceOptionsClient {
  HttpCommerceOptionsClient({
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
  Future<CommerceOptionsView> lookup(String keyword) async {
    final scope = householdContext.snapshot();
    final response = await client
        .get(
          baseUrl
              .resolve('/api/commerce/options')
              .replace(queryParameters: {'keyword': keyword}),
          headers: scope.headers,
        )
        .timeout(timeout);
    if (response.statusCode != 200) {
      throw StateError('Commerce request failed (${response.statusCode})');
    }

    final body = jsonDecode(response.body);
    if (body is! Map) {
      throw const FormatException('Commerce response must be an object');
    }
    final data = Map<String, dynamic>.from(body);
    final status = _statusFromJson(data['status']);
    if (status == null) {
      throw const FormatException('Commerce response has an invalid status');
    }
    final message = data['message'];
    final options = data['options'];
    if (options is! List) {
      throw const FormatException('Commerce options must be a list');
    }
    return CommerceOptionsView(
      status: status,
      message: message is String ? message : '',
      options: options
          .whereType<Map>()
          .map((option) => _optionFromJson(Map<String, dynamic>.from(option)))
          .whereType<CommerceOptionView>()
          .toList(growable: false),
    );
  }

  CommerceOptionsStatus? _statusFromJson(Object? value) => switch (value) {
    'success' => CommerceOptionsStatus.success,
    'no_match' => CommerceOptionsStatus.noMatch,
    'unavailable' => CommerceOptionsStatus.unavailable,
    _ => null,
  };

  CommerceOptionView? _optionFromJson(Map<String, dynamic> option) {
    final title = option['title'];
    if (title is! String || title.trim().isEmpty) return null;
    return CommerceOptionView(
      title: title.trim(),
      store: _nonBlankString(option['store']),
      source: _nonBlankString(option['source']),
      estimatedPrice: option['estimated_price'] is num
          ? option['estimated_price'] as num
          : null,
      productUrl: _nonBlankString(option['product_url']),
      unitSpec: _nonBlankString(option['unit_spec']),
      imageUrl: _nonBlankString(option['image_url']),
    );
  }

  String? _nonBlankString(Object? value) =>
      value is String && value.trim().isNotEmpty ? value.trim() : null;
}

CommerceOptionsClient commerceOptionsClientForTransport({
  required String transport,
  required Uri baseUrl,
  HouseholdRequestContext? householdContext,
  http.Client? client,
}) => transport == 'mock'
    ? const UnavailableCommerceOptionsClient()
    : HttpCommerceOptionsClient(
        baseUrl: baseUrl,
        householdContext:
            householdContext ??
            (throw ArgumentError('HTTP commerce requires household context.')),
        client: client,
      );
