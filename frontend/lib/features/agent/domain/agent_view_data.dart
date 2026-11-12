/// PROVISIONAL UI projections, not authoritative wire schemas or serializers.
enum ResultKind { inventory, vision, forecast, purchaseOptions, actionResult }

enum ApprovalPresentation {
  singleAction,
  batchAction,
  purchaseOptions,
  itemMatch,
}

class AgentResultView {
  const AgentResultView({
    required this.kind,
    required this.summary,
    this.data = const {},
  });
  final ResultKind kind;
  final String summary;
  final Map<String, dynamic> data;
}

class OperationView {
  const OperationView({
    required this.id,
    required this.description,
    this.action = '',
    this.values = const {},
  });
  final String id;
  final String action;
  final String description;
  final Map<String, dynamic> values;
}

class ApprovalOptionView {
  const ApprovalOptionView({
    required this.id,
    this.label,
    this.values = const {},
  });

  final String id;
  final String? label;
  final Map<String, dynamic> values;
}

class ApprovalView {
  const ApprovalView({
    required this.id,
    required this.presentation,
    required this.operations,
    this.options = const [],
  });
  final String id;
  final ApprovalPresentation presentation;
  final List<OperationView> operations;
  final List<ApprovalOptionView> options;
}

/// Local-only card content for the Confirmation Card presentation prototype.
/// This is not an approval wire model or a request to persist any change.
class ConfirmationItemView {
  const ConfirmationItemView({
    required this.name,
    required this.quantity,
    required this.unit,
    required this.location,
  });

  final String name;
  final num quantity;
  final String unit;
  final String location;
}

class AgentReply {
  const AgentReply({
    required this.message,
    this.results = const [],
    this.approval,
    this.mockVisionItems,
    this.mockConfirmationItems,
  });
  final String message;
  final List<AgentResultView> results;
  final ApprovalView? approval;

  /// Offline UI fixture only; not an A2 wire or persistence model.
  final List<({String name, String quantity, String location})>?
  mockVisionItems;

  /// Development-only Confirmation Card fixture; it has no backend lifecycle.
  final List<ConfirmationItemView>? mockConfirmationItems;
}
