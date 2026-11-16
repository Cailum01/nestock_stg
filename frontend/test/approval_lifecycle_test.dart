import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class _ApprovalEditClient implements AgentClient {
  _ApprovalEditClient({required this.replacement});

  final bool replacement;
  final List<(String message, String? replacesApprovalId)> sends = [];

  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async {
    sends.add((message, replacesApprovalId));
    if (replacesApprovalId == null) {
      return _reply(
        approvalId: 'approval-old',
        operationId: 'operation-old',
        description: '舊卡：會新增一個庫存品項：衛生紙',
      );
    }
    if (!replacement) return const AgentReply(message: '請補充更多資訊。');
    return _reply(
      approvalId: 'approval-new',
      operationId: 'operation-new',
      description: '新卡：會新增一個庫存品項：衛生紙',
    );
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => const AgentReply(message: '完成');
}

AgentReply _reply({
  required String approvalId,
  required String operationId,
  required String description,
}) => AgentReply(
  message: '請確認。',
  approval: ApprovalView(
    id: approvalId,
    presentation: ApprovalPresentation.singleAction,
    operations: [
      OperationView(
        id: operationId,
        action: 'create_item',
        description: description,
        values: const {
          'name': '衛生紙',
          'quantity': 2,
          'unit': '包',
          'location': '浴室櫃',
        },
      ),
    ],
  ),
);

Future<void> _openApproval(
  WidgetTester tester,
  _ApprovalEditClient client,
) async {
  await tester.pumpWidget(
    NestockApp(agentClient: client, catalog: const DemoCatalog()),
  );
  final input = find.descendant(
    of: find.byKey(const Key('composer')),
    matching: find.byType(TextField),
  );
  await tester.enterText(input, '衛生紙買回來了');
  await tester.tap(find.byKey(const Key('paw-send')));
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('confirmation-edit-0')));
  await tester.pump();
  await tester.enterText(find.byKey(const Key('confirmation-quantity-0')), '3');
  await tester.pump();
  await tester.tap(find.byKey(const Key('confirmation-submit-changes')));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('edit response without a pending approval keeps the old card', (
    tester,
  ) async {
    final client = _ApprovalEditClient(replacement: false);

    await _openApproval(tester, client);

    expect(client.sends, [('衛生紙買回來了', null), ('衛生紙：數量改成 3', 'approval-old')]);
    expect(find.text('舊卡：會新增一個庫存品項：衛生紙'), findsOneWidget);
    expect(find.text('請補充更多資訊。'), findsOneWidget);
    expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
  });

  testWidgets('edit response with a pending approval replaces the old card', (
    tester,
  ) async {
    final client = _ApprovalEditClient(replacement: true);

    await _openApproval(tester, client);

    expect(find.text('舊卡：會新增一個庫存品項：衛生紙'), findsNothing);
    expect(find.text('新卡：會新增一個庫存品項：衛生紙'), findsOneWidget);
    expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
  });
}
