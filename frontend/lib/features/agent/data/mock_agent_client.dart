import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';

/// Offline presentation demo: no inference, network, inventory or shopping writes.
class MockAgentClient implements AgentClient {
  int _sequence = 0;

  @override
  Future<AgentReply> send(String message) async {
    final identity = 'demo-${++_sequence}';
    return AgentReply(
      message: '這是固定示範回覆。你可以開啟櫃子查看庫存，或預覽下方確認流程。',
      results: const [
        AgentResultView(
          kind: ResultKind.inventory,
          summary: '示範庫存：白米 2 包，位於廚房櫃子。',
        ),
      ],
      approval: ApprovalView(
        id: 'approval-$identity',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-$identity',
            description: '示範家庭 demo-household：新增牛奶 1 瓶至購物清單（待購買）。',
          ),
        ],
      ),
    );
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  }) async => AgentReply(
    message: approved ? '確認流程示範結束，沒有儲存任何變更。' : '已取消示範，沒有儲存任何變更。',
    results: const [
      AgentResultView(kind: ResultKind.actionResult, summary: '庫存與購物清單保持不變。'),
    ],
  );
}
