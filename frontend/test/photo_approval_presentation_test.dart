import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/agent_result_card.dart';
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

const _photoCreateApproval = ApprovalView(
  id: 'photo-create-approval',
  presentation: ApprovalPresentation.singleAction,
  operations: [
    OperationView(
      id: 'photo-create-operation',
      action: 'create_item',
      description: '會新增一個庫存品項：張國周強胃散 1 罐（照片辨識）',
      values: {'name': '張國周強胃散', 'quantity': 1, 'unit': '罐', 'location': null},
    ),
  ],
);

const _photoExistingIncreaseApproval = ApprovalView(
  id: 'photo-increase-approval',
  presentation: ApprovalPresentation.singleAction,
  operations: [
    OperationView(
      id: 'photo-increase-operation',
      action: increaseStockApprovalAction,
      description: '羅技 G400s 有線滑鼠 +1 個（家裡已有的品項）（照片辨識）',
      values: {
        'item_id': 'inventory-mouse',
        'name': '羅技 G400s 有線滑鼠',
        'quantity_added': 1,
        'unit': '個',
        'transaction_id': 'transaction-mouse',
        'reason': 'adjustment',
        'inventory_quantity_before': 1,
      },
    ),
  ],
);

Future<void> _pumpReply(WidgetTester tester, AgentReply reply) async {
  await tester.pumpWidget(
    NestockApp(agentClient: _ReplyClient(reply), catalog: const DemoCatalog()),
  );
  await tester.enterText(
    find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    ),
    '請辨識照片',
  );
  await tester.tap(find.byKey(const Key('paw-send')));
  await tester.pumpAndSettle();
}

void main() {
  const visionResult = AgentResultView(
    kind: ResultKind.vision,
    summary: '照片辨識完成',
    data: {
      'items': [
        {'name': '張國周強胃散', 'quantity': 1, 'unit': '罐', 'unit_confirmed': true},
      ],
    },
  );

  testWidgets('photo create approval suppresses its redundant Vision result', (
    tester,
  ) async {
    await _pumpReply(
      tester,
      const AgentReply(
        message: '請確認建立庫存品項。',
        results: [visionResult],
        approval: _photoCreateApproval,
      ),
    );

    expect(find.byKey(const Key('vision-result-card')), findsNothing);
    expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
    expect(find.text('確認建立庫存品項'), findsOneWidget);
  });

  testWidgets(
    'photo existing-item increase hides the new-item result and metadata',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '請確認增加家中既有品項的數量。',
          results: [
            AgentResultView(
              kind: ResultKind.vision,
              summary: '照片辨識完成',
              data: {
                'items': [
                  {
                    'name': '羅技 G400s 有線滑鼠',
                    'quantity': 1,
                    'unit': '個',
                    'unit_confirmed': true,
                  },
                ],
              },
            ),
          ],
          approval: _photoExistingIncreaseApproval,
        ),
      );

      expect(find.byKey(const Key('vision-result-card')), findsNothing);
      expect(find.text('尚未加入庫存'), findsNothing);
      expect(
        find.byKey(const Key('increase-stock-approval-card')),
        findsOneWidget,
      );
      expect(find.text('確認增加庫存'), findsOneWidget);
      expect(find.text('羅技 G400s 有線滑鼠'), findsOneWidget);
      expect(find.text('增加 1 個'), findsOneWidget);
      expect(find.text('家裡已有的品項'), findsOneWidget);
      expect(find.textContaining('item_id'), findsNothing);
      expect(find.textContaining('transaction_id'), findsNothing);
      expect(find.textContaining('reason'), findsNothing);
      expect(find.textContaining('inventory_quantity_before'), findsNothing);
      expect(
        find.byKey(const Key('approval-cancel')).hitTestable(),
        findsOneWidget,
      );
      expect(
        find.byKey(const Key('approval-confirm')).hitTestable(),
        findsOneWidget,
      );
    },
  );

  testWidgets(
    'photo existing-item increase suppresses an aliased Vision result by matched household name',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '請確認增加家中既有品項的數量。',
          results: [
            AgentResultView(
              kind: ResultKind.vision,
              summary: '照片辨識完成',
              data: {
                'items': [
                  {
                    'name': '滑鼠',
                    'seen_as': '滑鼠',
                    'match': {'item_name': '羅技 G400s 有線滑鼠'},
                    'quantity': 1,
                    'unit': '個',
                  },
                ],
              },
            ),
          ],
          approval: _photoExistingIncreaseApproval,
        ),
      );

      expect(find.byKey(const Key('vision-result-card')), findsNothing);
      expect(
        find.byKey(const Key('increase-stock-approval-card')),
        findsOneWidget,
      );
      expect(find.text('尚未加入庫存'), findsNothing);
    },
  );

  testWidgets(
    'Vision result remains visible when its matched household name is unrelated',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '請確認增加家中既有品項的數量。',
          results: [
            AgentResultView(
              kind: ResultKind.vision,
              summary: '照片辨識完成',
              data: {
                'items': [
                  {
                    'name': '滑鼠',
                    'match': {'item_name': '其他家庭品項'},
                    'quantity': 1,
                    'unit': '個',
                  },
                ],
              },
            ),
          ],
          approval: _photoExistingIncreaseApproval,
        ),
      );

      expect(find.byKey(const Key('vision-result-card')), findsOneWidget);
      expect(
        find.byKey(const Key('increase-stock-approval-card')),
        findsOneWidget,
      );
    },
  );

  testWidgets('Vision result remains visible without a pending approval', (
    tester,
  ) async {
    await _pumpReply(
      tester,
      const AgentReply(message: '照片辨識完成。', results: [visionResult]),
    );

    expect(find.byKey(const Key('vision-result-card')), findsOneWidget);
    expect(find.text('尚未加入庫存'), findsOneWidget);
  });

  testWidgets(
    'Vision result remains visible when it also contains an unrelated observation',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '請確認建立庫存品項。',
          results: [
            AgentResultView(
              kind: ResultKind.vision,
              summary: '照片辨識完成',
              data: {
                'items': [
                  {
                    'name': '張國周強胃散',
                    'quantity': 1,
                    'unit': '罐',
                    'unit_confirmed': true,
                  },
                  {
                    'name': '維他命C',
                    'quantity': 1,
                    'unit': '瓶',
                    'unit_confirmed': true,
                  },
                ],
              },
            ),
          ],
          approval: _photoCreateApproval,
        ),
      );

      expect(find.byKey(const Key('vision-result-card')), findsOneWidget);
      expect(find.text('維他命C'), findsOneWidget);
      expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
    },
  );

  testWidgets(
    'non-Vision result remains visible with a photo create approval',
    (tester) async {
      await _pumpReply(
        tester,
        const AgentReply(
          message: '請確認建立庫存品項。',
          results: [
            AgentResultView(
              kind: ResultKind.inventory,
              summary: '目前庫存',
              data: {
                'items': [
                  {'name': '白米', 'quantity': 2, 'unit': '包'},
                ],
              },
            ),
          ],
          approval: _photoCreateApproval,
        ),
      );

      expect(find.byType(AgentResultCard), findsOneWidget);
      expect(find.text('庫存'), findsOneWidget);
      expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
    },
  );
}
