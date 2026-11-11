import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/approval_card.dart';

void main() {
  Future<void> pumpCard(
    WidgetTester tester,
    ApprovalView approval, {
    ApprovalDecisionCallback? onDecision,
    ValueChanged<String>? onEdit,
  }) {
    return tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ApprovalCard(
            approval: approval,
            busy: false,
            onDecision:
                onDecision ?? ({required approved, selectedOptionId}) {},
            onSubmitChanges: onEdit,
          ),
        ),
      ),
    );
  }

  testWidgets(
    'item match shows backend content and returns the stable option ID',
    (tester) async {
      bool? didApprove;
      String? selectedOption;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-match-1',
          presentation: ApprovalPresentation.itemMatch,
          operations: [
            OperationView(
              id: 'operation-match-1',
              action: 'confirm_item_match',
              description: '這是家裡的洗潔精嗎？',
              values: {
                'user_term': '洗碗精',
                'candidate_item_id': 'item-cleanser',
                'candidate_name': '洗潔精',
                'quantity': 1,
                'unit': '瓶',
              },
            ),
          ],
          options: [
            ApprovalOptionView(id: 'same_item', label: '是，加到洗潔精'),
            ApprovalOptionView(id: 'new_item', label: '不是，這是新的東西'),
          ],
        ),
        onDecision: ({required approved, selectedOptionId}) {
          didApprove = approved;
          selectedOption = selectedOptionId;
        },
      );

      expect(find.byKey(const Key('item-match-approval-card')), findsOneWidget);
      expect(find.text('這是家裡的洗潔精嗎？'), findsOneWidget);
      expect(find.text('你說的是：洗碗精'), findsOneWidget);
      expect(find.text('家中品項：洗潔精'), findsOneWidget);
      expect(find.text('item-cleanser'), findsNothing);

      await tester.tap(find.byKey(const Key('approval-option-new_item')));

      expect(didApprove, isTrue);
      expect(selectedOption, 'new_item');
    },
  );

  testWidgets(
    'create item uses the Confirmation Card and submits deterministic edits',
    (tester) async {
      String? editMessage;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-create-1',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-create-1',
              action: 'create_item',
              description: '會新增一個庫存品項：洗碗精',
              values: {
                'name': '洗碗精',
                'quantity': 1,
                'unit': '瓶',
                'location': '廚房櫃子',
              },
            ),
          ],
        ),
        onEdit: (message) => editMessage = message,
      );

      expect(find.text('確認建立庫存品項'), findsOneWidget);
      expect(find.text('會新增一個庫存品項：洗碗精'), findsOneWidget);
      expect(find.text('洗碗精'), findsOneWidget);
      await tester.tap(find.byKey(const Key('confirmation-edit-0')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('confirmation-quantity-0')),
        '3',
      );
      await tester.enterText(find.byKey(const Key('confirmation-unit-0')), '包');
      await tester.enterText(
        find.byKey(const Key('confirmation-location-0')),
        '浴室櫃',
      );
      await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

      expect(editMessage, '洗碗精：數量改成 3，單位改成 包，位置改成 浴室櫃');
    },
  );

  test('edit messages are stable and only include changed fields', () {
    const original = ConfirmationItemView(
      name: '衛生紙',
      quantity: 2,
      unit: '包',
      location: '浴室櫃',
    );
    expect(
      buildApprovalEditMessage(
        original: original,
        edited: const ConfirmationItemView(
          name: '衛生紙',
          quantity: 3,
          unit: '包',
          location: '浴室櫃',
        ),
      ),
      '衛生紙：數量改成 3',
    );
    expect(
      buildApprovalEditMessage(
        original: original,
        edited: const ConfirmationItemView(
          name: '衛生紙',
          quantity: 3,
          unit: '包',
          location: '客廳櫃',
        ),
      ),
      '衛生紙：數量改成 3，位置改成 客廳櫃',
    );
  });
}
