import 'dart:convert';

import 'package:http/http.dart' as http;

import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';

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
  Future<AgentReply> send(String message, {String? replacesApprovalId}) =>
      _send(message, null, replacesApprovalId: replacesApprovalId);

  @override
  Future<AgentReply> sendWithPhoto(String message, String imageRef) =>
      _send(message, imageRef);

  Future<AgentReply> _send(
    String message,
    String? imageRef, {
    String? replacesApprovalId,
  }) async {
    final response = await client
        .post(
          baseUrl.resolve('/agent/messages'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': 'demo-household',
            'message': message,
            'image_ref': ?imageRef,
            'replaces_approval_id': ?replacesApprovalId,
          }),
        )
        .timeout(timeout);
    if (response.statusCode != 200) throw StateError('Agent request failed');
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Agent response must be an object');
    }
    return _replyFromBody(Map<String, dynamic>.from(decoded));
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async {
    final response = await client
        .post(
          baseUrl.resolve('/agent/resume'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': 'demo-household',
            'approval_id': approvalId,
            'approved': approved,
            'selected_option_id': ?selectedOptionId,
            'approved_operation_ids': ?approvedOperationIds,
          }),
        )
        .timeout(timeout);
    if (response.statusCode != 200) {
      throw StateError('Approval request failed');
    }
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Agent response must be an object');
    }
    return _replyFromBody(Map<String, dynamic>.from(decoded));
  }

  AgentReply _replyFromBody(Map<String, dynamic> body) {
    final message = body['message'];
    if (message is! String) {
      throw const FormatException('Agent response message must be a string');
    }
    final rawResults = body['results'];
    if (rawResults != null && rawResults is! List) {
      throw const FormatException('Agent response results must be a list');
    }
    final results = (rawResults as List<dynamic>? ?? [])
        .map((value) {
          if (value is! Map) {
            throw const FormatException('Agent result must be an object');
          }
          final result = Map<String, dynamic>.from(value);
          final kind = switch (result['kind']) {
            'inventory' => ResultKind.inventory,
            'vision' => ResultKind.vision,
            'forecast' => ResultKind.forecast,
            'purchase_options' => ResultKind.purchaseOptions,
            'action_result' => ResultKind.actionResult,
            _ => throw const FormatException('Unknown result kind'),
          };
          final summary = result['summary'];
          if (summary is! String) {
            throw const FormatException(
              'Agent result summary must be a string',
            );
          }
          final data = result['data'];
          if (data != null && data is! Map) {
            throw const FormatException('Agent result data must be an object');
          }
          return AgentResultView(
            kind: kind,
            summary: summary,
            data: Map<String, dynamic>.from(data as Map? ?? const {}),
          );
        })
        .toList(growable: false);
    return AgentReply(
      message: message,
      results: results,
      approval: _approvalFromJson(body['pending_approval']),
    );
  }

  ApprovalView? _approvalFromJson(dynamic rawApproval) {
    if (rawApproval == null) return null;
    if (rawApproval is! Map) {
      throw const FormatException('Pending approval must be an object');
    }
    final approval = Map<String, dynamic>.from(rawApproval);
    final id = approval['approval_id'];
    final presentation = _presentationFromJson(approval['presentation']);
    final rawOperations = approval['operations'];
    if (id is! String || id.trim().isEmpty || rawOperations is! List) {
      throw const FormatException('Pending approval is malformed');
    }
    final operations = rawOperations
        .map(_operationFromJson)
        .toList(growable: false);
    if (operations.isEmpty) {
      throw const FormatException('Pending approval must contain an operation');
    }
    final rawOptions = approval['options'];
    if (rawOptions != null && rawOptions is! List) {
      throw const FormatException('Approval options must be a list');
    }
    final options = (rawOptions as List<dynamic>? ?? [])
        .map(_optionFromJson)
        .toList(growable: false);
    if (presentation == ApprovalPresentation.itemMatch &&
        options.any((option) => option.label == null)) {
      throw const FormatException('Item-match options require a label');
    }
    return ApprovalView(
      id: id,
      presentation: presentation,
      operations: operations,
      options: options,
    );
  }

  ApprovalPresentation _presentationFromJson(dynamic value) => switch (value) {
    'single_action' => ApprovalPresentation.singleAction,
    'batch_action' => ApprovalPresentation.batchAction,
    'purchase_options' => ApprovalPresentation.purchaseOptions,
    'item_match' => ApprovalPresentation.itemMatch,
    _ => throw const FormatException('Unknown approval presentation'),
  };

  OperationView _operationFromJson(dynamic rawOperation) {
    if (rawOperation is! Map) {
      throw const FormatException('Approval operation must be an object');
    }
    final operation = Map<String, dynamic>.from(rawOperation);
    final id = operation['operation_id'];
    final action = operation['action'];
    final description = operation['description'];
    final values = operation['values'];
    if (id is! String ||
        id.trim().isEmpty ||
        action is! String ||
        action.trim().isEmpty ||
        description is! String ||
        values is! Map) {
      throw const FormatException('Approval operation is malformed');
    }
    return OperationView(
      id: id,
      action: action,
      description: description,
      values: Map<String, dynamic>.from(values),
    );
  }

  ApprovalOptionView _optionFromJson(dynamic rawOption) {
    if (rawOption is! Map) {
      throw const FormatException('Approval option must be an object');
    }
    final option = Map<String, dynamic>.from(rawOption);
    final id = option['option_id'];
    final label = option['label'];
    if (id is! String ||
        id.trim().isEmpty ||
        label != null && label is! String) {
      throw const FormatException('Approval option is malformed');
    }
    final values = Map<String, dynamic>.from(option)..remove('option_id');
    return ApprovalOptionView(
      id: id,
      label: label is String && label.trim().isNotEmpty ? label.trim() : null,
      values: values,
    );
  }
}
