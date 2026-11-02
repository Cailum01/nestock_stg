/// PROVISIONAL UI projections, not authoritative wire schemas or serializers.
enum ResultKind { inventory, vision, forecast, purchaseOptions, actionResult }

enum ApprovalPresentation { singleAction, batchAction, purchaseOptions }

class AgentResultView {
  const AgentResultView({required this.kind, required this.summary});
  final ResultKind kind;
  final String summary;
}

class OperationView {
  const OperationView({required this.id, required this.description});
  final String id;
  // Mock-only text. Real approvals must render every resolved relevant value.
  final String description;
}

class ApprovalView {
  const ApprovalView({
    required this.id,
    required this.presentation,
    required this.operations,
  });
  final String id;
  final ApprovalPresentation presentation;
  final List<OperationView> operations;
}

class AgentReply {
  const AgentReply({
    required this.message,
    this.results = const [],
    this.approval,
    this.mockVisionItems,
  });
  final String message;
  final List<AgentResultView> results;
  final ApprovalView? approval;

  /// Offline UI fixture only; not an A2 wire or persistence model.
  final List<({String name, String quantity, String location})>?
  mockVisionItems;
}
