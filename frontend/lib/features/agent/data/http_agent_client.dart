import 'dart:convert';

import 'package:http/http.dart' as http;

import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import '../../onboarding/domain/household_context.dart';
import '../../onboarding/data/secure_transport_client.dart';

class HttpAgentClient
    implements PhotoAwareAgentClient, HouseholdBoundAgentClient {
  HttpAgentClient({
    required this.baseUrl,
    required this.householdContext,
    http.Client? client,
    this.timeout = const Duration(seconds: 75),
  }) : client = SecureTransportClient(innerClient: client);
  final Uri baseUrl;
  final HouseholdRequestContext householdContext;
  final http.Client client;
  final Duration timeout;

  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) =>
      _send(message, null, replacesApprovalId: replacesApprovalId);

  @override
  Future<AgentReply> sendWithPhoto(String message, String imageRef) =>
      _send(message, imageRef);

  @override
  Future<AgentReply> submitCardChanges({
    required String approvalId,
    required List<CardChange> changes,
  }) =>
      _send('送出修改', null, replacesApprovalId: approvalId, cardChanges: changes);

  @override
  Future<AgentReply> submitCardChangesInHousehold({
    required String approvalId,
    required List<CardChange> changes,
    required String householdId,
  }) => _send(
    '送出修改',
    null,
    replacesApprovalId: approvalId,
    cardChanges: changes,
    householdIdOverride: householdId,
  );

  Future<AgentReply> _send(
    String message,
    String? imageRef, {
    String? replacesApprovalId,
    List<CardChange>? cardChanges,
    String? householdIdOverride,
  }) async {
    final scope = householdContext.snapshot(
      householdIdOverride: householdIdOverride,
    );
    final response = await client
        .post(
          baseUrl.resolve('/agent/messages'),
          headers: {...scope.headers, 'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': scope.householdId,
            'message': message,
            'image_ref': ?imageRef,
            'replaces_approval_id': ?replacesApprovalId,
            'card_changes': ?cardChanges
                ?.map((change) => change.toJson())
                .toList(growable: false),
          }),
        )
        .timeout(timeout);
    if (response.statusCode != 200) throw StateError('Agent request failed');
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Agent response must be an object');
    }
    return _replyFromBody(
      Map<String, dynamic>.from(decoded),
      householdId: scope.householdId,
    );
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async {
    return _resume(
      approvalId: approvalId,
      approved: approved,
      selectedOptionId: selectedOptionId,
      approvedOperationIds: approvedOperationIds,
    );
  }

  @override
  Future<AgentReply> resumeInHousehold({
    required String approvalId,
    required bool approved,
    required String householdId,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) => _resume(
    approvalId: approvalId,
    approved: approved,
    householdIdOverride: householdId,
    selectedOptionId: selectedOptionId,
    approvedOperationIds: approvedOperationIds,
  );

  Future<AgentReply> _resume({
    required String approvalId,
    required bool approved,
    String? householdIdOverride,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async {
    final scope = householdContext.snapshot(
      householdIdOverride: householdIdOverride,
    );
    final response = await client
        .post(
          baseUrl.resolve('/agent/resume'),
          headers: {...scope.headers, 'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': scope.householdId,
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
    return _replyFromBody(
      Map<String, dynamic>.from(decoded),
      householdId: scope.householdId,
    );
  }

  AgentReply _replyFromBody(
    Map<String, dynamic> body, {
    required String householdId,
  }) {
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
      approval: _approvalFromJson(
        body['pending_approval'],
        householdId: householdId,
      ),
    );
  }

  ApprovalView? _approvalFromJson(
    dynamic rawApproval, {
    required String householdId,
  }) {
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
      householdId: householdId,
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
