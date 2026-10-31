import 'agent_view_data.dart';

/// Provisional household-scoped application port. No LangGraph or HTTP details.
/// A1/C2 will agree the wire adapter against backend Pydantic/OpenAPI models.
abstract interface class AgentClient {
  Future<AgentReply> send(String message);

  /// Retry this same approval identity after a timeout; never mint a new write.
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  });
}
