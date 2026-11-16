import 'dart:async';

import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';

/// Local-only presentation scenarios. These are not backend status values.
enum MockScenario {
  conversation,
  completed,
  singleAction,
  batchAction,
  purchaseOptions,
  partialSuccess,
  error,
  timeout,
}

class MockAgentClient implements AgentClient {
  MockAgentClient({this.scenario = MockScenario.singleAction});
  final MockScenario scenario;
  int _sequence = 0;

  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async {
    if (scenario == MockScenario.conversation) {
      return const AgentReply(message: '這是對話介面示範回覆。');
    }
    if (scenario == MockScenario.error) throw StateError('demo error');
    if (scenario == MockScenario.timeout) {
      throw TimeoutException('demo timeout');
    }
    if (scenario == MockScenario.completed ||
        scenario == MockScenario.partialSuccess) {
      return AgentReply(
        message: scenario == MockScenario.completed
            ? '示範查詢已完成。'
            : '部分結果已顯示，其餘暫時無法取得。',
        results: [
          AgentResultView(
            kind: scenario == MockScenario.completed
                ? ResultKind.inventory
                : ResultKind.actionResult,
            summary: scenario == MockScenario.completed
                ? '示範庫存：白米 2 包。'
                : '部分成功（示範）：沒有儲存任何變更。',
          ),
        ],
      );
    }
    final identity = 'demo-${++_sequence}';
    final presentation = switch (scenario) {
      MockScenario.batchAction => ApprovalPresentation.batchAction,
      MockScenario.purchaseOptions => ApprovalPresentation.purchaseOptions,
      _ => ApprovalPresentation.singleAction,
    };
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
        presentation: presentation,
        operations: [
          OperationView(
            id: 'operation-$identity-1',
            description: '示範家庭 demo-household：新增牛奶 1 瓶至購物清單（待購買）。',
          ),
          if (presentation != ApprovalPresentation.singleAction)
            OperationView(
              id: 'operation-$identity-2',
              description: '示範家庭 demo-household：新增白米 1 包至購物清單（待購買）。',
            ),
        ],
      ),
    );
  }

  @override
  Future<AgentReply> submitCardChanges({
    required String approvalId,
    required List<CardChange> changes,
  }) => send('送出修改', replacesApprovalId: approvalId);

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => AgentReply(
    message: approved ? '確認流程示範結束，沒有儲存任何變更。' : '已取消示範，沒有儲存任何變更。',
    results: const [
      AgentResultView(kind: ResultKind.actionResult, summary: '庫存與購物清單保持不變。'),
    ],
  );
}
