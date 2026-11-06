import 'dart:convert';

import 'package:http/http.dart' as http;

import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import 'mock_agent_client.dart';

class HttpAgentClient implements PhotoAwareAgentClient {
  HttpAgentClient({
    required this.baseUrl,
    http.Client? client,
    this.timeout = const Duration(seconds: 75),
  }) : client = client ?? http.Client();
  final Uri baseUrl;
  final http.Client client;
  final Duration timeout;

  @override
  Future<AgentReply> send(String message) => _send(message, null);

  @override
  Future<AgentReply> sendWithPhoto(String message, String imageRef) =>
      _send(message, imageRef);

  Future<AgentReply> _send(String message, String? imageRef) async {
    if ((message == '/vision-demo' || message == '/confirmation-demo') &&
        imageRef == null) {
      return MockAgentClient().send(message);
    }
    final response = await client
        .post(
          baseUrl.resolve('/agent/messages'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': 'demo-household',
            'message': message,
            'image_ref': ?imageRef,
          }),
        )
        .timeout(timeout);
    if (response.statusCode != 200) throw StateError('Agent request failed');
    final body = jsonDecode(response.body) as Map<String, dynamic>;
    if (body['pending_approval'] != null) {
      throw UnsupportedError('Approval is not supported');
    }
    final results = (body['results'] as List<dynamic>? ?? []).map((value) {
      final result = value as Map<String, dynamic>;
      final kind = switch (result['kind']) {
        'inventory' => ResultKind.inventory,
        'vision' => ResultKind.vision,
        'forecast' => ResultKind.forecast,
        'purchase_options' => ResultKind.purchaseOptions,
        'action_result' => ResultKind.actionResult,
        _ => throw const FormatException('Unknown result kind'),
      };
      return AgentResultView(
        kind: kind,
        summary: result['summary'] as String,
        data: Map<String, dynamic>.from(result['data'] as Map? ?? const {}),
      );
    }).toList();
    return AgentReply(message: body['message'] as String, results: results);
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  }) async => throw UnsupportedError('Approval is not supported');
}
