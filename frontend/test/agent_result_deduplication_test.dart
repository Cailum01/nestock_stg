import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class _ReplyClient implements AgentClient {
  const _ReplyClient(this.reply);

  final AgentReply reply;

  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async =>
      reply;

  @override
  Future<AgentReply> submitCardChanges({
    required String approvalId,
    required List<CardChange> changes,
  }) async => reply;

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => reply;
}

Future<void> _pumpReply(WidgetTester tester, AgentReply reply) async {
  await tester.pumpWidget(
    NestockApp(agentClient: _ReplyClient(reply), catalog: const DemoCatalog()),
  );
  await tester.enterText(
    find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    ),
    '測試結果呈現',
  );
  await tester.tap(find.byKey(const Key('paw-send')));
  await tester.pumpAndSettle();
}

void main() {
  const expiredMessage = '這張確認卡已經過期，沒有記錄。請再說一次要記錄的內容。';

  testWidgets(
    'hides an action result that exactly duplicates the Agent message',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: expiredMessage,
          results: [
            AgentResultView(
              kind: ResultKind.actionResult,
              summary: expiredMessage,
            ),
          ],
        ),
      );

      expect(find.text(expiredMessage), findsOneWidget);
      expect(find.text('操作結果'), findsNothing);
    },
  );

  testWidgets(
    'trims action-result and Agent message text before deduplicating',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '  已取消這張確認卡。  ',
          results: [
            AgentResultView(
              kind: ResultKind.actionResult,
              summary: '已取消這張確認卡。',
            ),
          ],
        ),
      );

      expect(find.text('  已取消這張確認卡。  '), findsOneWidget);
      expect(find.text('操作結果'), findsNothing);
    },
  );

  testWidgets('keeps an action result with additional information', (
    tester,
  ) async {
    await _pumpReply(
      tester,
      const AgentReply(
        message: '確認卡已取消。',
        results: [
          AgentResultView(
            kind: ResultKind.actionResult,
            summary: '確認卡已取消，庫存沒有變更。',
          ),
        ],
      ),
    );

    expect(find.text('確認卡已取消。'), findsOneWidget);
    expect(find.text('操作結果'), findsOneWidget);
    expect(find.text('確認卡已取消，庫存沒有變更。'), findsOneWidget);
  });

  testWidgets('keeps a non-action result with the same summary', (
    tester,
  ) async {
    await _pumpReply(
      tester,
      const AgentReply(
        message: '目前庫存已更新。',
        results: [
          AgentResultView(kind: ResultKind.inventory, summary: '目前庫存已更新。'),
        ],
      ),
    );

    expect(find.text('目前庫存已更新。'), findsNWidgets(2));
    expect(find.text('庫存'), findsOneWidget);
  });
}
