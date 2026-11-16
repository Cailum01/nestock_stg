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
    Future<void> Function(Uri url)? onOpenProductUrl,
  }) {
    return tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ApprovalCard(
            approval: approval,
            busy: false,
            onDecision:
                onDecision ??
                ({
                  required approved,
                  selectedOptionId,
                  approvedOperationIds,
                }) {},
            onSubmitChanges: onEdit,
            onOpenProductUrl: onOpenProductUrl,
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
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
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

  testWidgets(
    'create item without a location uses a display placeholder and keeps approval decisions unchanged',
    (tester) async {
      final decisions = <bool>[];
      List<String>? operationIds;
      const approval = ApprovalView(
        id: 'approval-create-no-location',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-create-no-location',
            action: 'create_item',
            description: '會新增一個庫存品項：滑鼠',
            values: {
              'name': '滑鼠',
              'quantity': 1,
              'unit': '個',
              'location': null,
              'transaction_id': 'opaque-transaction-id',
            },
          ),
        ],
      );

      await pumpCard(
        tester,
        approval,
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              decisions.add(approved);
              operationIds = approvedOperationIds;
            },
      );

      expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
      expect(find.text('未填'), findsOneWidget);
      expect(find.text('opaque-transaction-id'), findsNothing);
      expect(find.textContaining('transaction_id'), findsNothing);

      await tester.tap(find.byKey(const Key('confirmation-confirm')));
      expect(decisions, [true]);
      expect(operationIds, isNull);

      await pumpCard(
        tester,
        approval,
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              decisions.add(approved);
            },
      );
      await tester.tap(find.byKey(const Key('confirmation-cancel')));
      expect(decisions, [true, false]);
    },
  );

  testWidgets('missing or blank create-item location displays 未填', (
    tester,
  ) async {
    for (final locationValues in <Map<String, dynamic>>[
      const {},
      const {'location': ''},
      const {'location': '  '},
    ]) {
      await pumpCard(
        tester,
        ApprovalView(
          id: 'approval-create-location-${locationValues.length}',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-create-location-${locationValues.length}',
              action: 'create_item',
              description: '會新增一個庫存品項：滑鼠',
              values: {
                'name': '滑鼠',
                'quantity': 1,
                'unit': '個',
                ...locationValues,
              },
            ),
          ],
        ),
      );

      expect(find.byKey(const Key('confirmation-card-shell')), findsOneWidget);
      expect(find.text('未填'), findsOneWidget);
    }
  });

  testWidgets('create item preserves a valid location and never submits 未填', (
    tester,
  ) async {
    String? editMessage;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-create-valid-location',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-create-valid-location',
            action: 'create_item',
            description: '會新增一個庫存品項：滑鼠',
            values: {
              'name': '滑鼠',
              'quantity': 1,
              'unit': '個',
              'location': '書桌抽屜',
            },
          ),
        ],
      ),
      onEdit: (message) => editMessage = message,
    );

    expect(find.text('書桌抽屜'), findsOneWidget);
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('confirmation-quantity-0')),
      '2',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

    expect(editMessage, '滑鼠：數量改成 2');
    expect(editMessage, isNot(contains('未填')));
  });

  testWidgets('blank optional location stays null in an edit request', (
    tester,
  ) async {
    String? editMessage;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-create-blank-edit',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-create-blank-edit',
            action: 'create_item',
            description: '會新增一個庫存品項：滑鼠',
            values: {
              'name': '滑鼠',
              'quantity': 1,
              'unit': '個',
              'location': null,
            },
          ),
        ],
      ),
      onEdit: (message) => editMessage = message,
    );

    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('confirmation-quantity-0')),
      '2',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

    expect(editMessage, '滑鼠：數量改成 2');
    expect(editMessage, isNot(contains('未填')));
  });

  testWidgets(
    'batch create items keep selection and submit selected operation IDs',
    (tester) async {
      List<String>? selectedOperationIds;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-create-batch',
          presentation: ApprovalPresentation.batchAction,
          operations: [
            OperationView(
              id: 'operation-create-mouse',
              action: 'create_item',
              description: '建立庫存品項：滑鼠',
              values: {
                'name': '滑鼠',
                'quantity': 1,
                'unit': '個',
                'location': null,
                'transaction_id': 'opaque-mouse-transaction',
              },
            ),
            OperationView(
              id: 'operation-create-keyboard',
              action: 'create_item',
              description: '建立庫存品項：鍵盤',
              values: {
                'name': '鍵盤',
                'quantity': 1,
                'unit': '個',
                'location': null,
                'transaction_id': 'opaque-keyboard-transaction',
              },
            ),
          ],
        ),
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              selectedOperationIds = approvedOperationIds;
            },
      );

      expect(
        find.byKey(const Key('generic-batch-approval-card')),
        findsOneWidget,
      );
      expect(find.text('建立庫存品項：滑鼠'), findsOneWidget);
      expect(find.text('建立庫存品項：鍵盤'), findsOneWidget);
      expect(find.textContaining('opaque-'), findsNothing);

      await tester.tap(
        find.byKey(
          const Key('generic-batch-operation-operation-create-keyboard'),
        ),
      );
      await tester.tap(find.byKey(const Key('approval-confirm')));

      expect(selectedOperationIds, ['operation-create-mouse']);
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

  test('purchase quantity edit message changes only the quantity', () {
    expect(
      buildPurchaseQuantityEditMessage(
        name: '衛生紙',
        originalQuantity: 2,
        editedQuantity: 3,
      ),
      '衛生紙：數量改成 3',
    );
    expect(
      buildPurchaseQuantityEditMessage(
        name: '衛生紙',
        originalQuantity: 2,
        editedQuantity: 2,
      ),
      isNull,
    );
  });

  testWidgets(
    'purchase card displays only purchase values and edits quantity',
    (tester) async {
      String? editMessage;
      bool? approvalWasAccepted;
      List<String>? operationIds;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-purchase-1',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-purchase-1',
              action: 'complete_purchase',
              description: '衛生紙 +2 包',
              values: {
                'name': '衛生紙',
                'purchased_quantity': 2,
                'purchased_unit': '包',
                'shopping_item_id': 'shopping-1',
                'item_id': 'item-1',
                'transaction_id': 'transaction-1',
              },
            ),
          ],
        ),
        onEdit: (message) => editMessage = message,
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              approvalWasAccepted = approved;
              operationIds = approvedOperationIds;
            },
      );

      expect(find.byKey(const Key('purchase-approval-card')), findsOneWidget);
      expect(find.text('衛生紙 2 包'), findsOneWidget);
      expect(find.text('shopping-1'), findsNothing);
      expect(find.text('item-1'), findsNothing);
      expect(find.text('transaction-1'), findsNothing);
      expect(find.text('位置'), findsNothing);
      expect(find.byKey(const Key('confirmation-name-0')), findsNothing);
      expect(find.byKey(const Key('confirmation-unit-0')), findsNothing);
      await tester.tap(find.byKey(const Key('purchase-edit-quantity')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('purchase-quantity-field')),
        '3',
      );
      await tester.pump();
      await tester.tap(
        find.byKey(const Key('purchase-submit-quantity-change')),
      );
      expect(editMessage, '衛生紙：數量改成 3');

      await tester.tap(find.byKey(const Key('approval-confirm')));
      expect(approvalWasAccepted, isTrue);
      expect(operationIds, isNull);
    },
  );

  testWidgets('purchase quantity editor permits only positive integers', (
    tester,
  ) async {
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-validation',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-purchase-validation',
            action: 'complete_purchase',
            description: '衛生紙 +2 包',
            values: {
              'name': '衛生紙',
              'purchased_quantity': 2,
              'purchased_unit': '包',
            },
          ),
        ],
      ),
    );

    await tester.tap(find.byKey(const Key('purchase-edit-quantity')));
    await tester.pump();
    final field = find.byKey(const Key('purchase-quantity-field'));
    expect(
      find.byKey(const Key('purchase-submit-quantity-change')),
      findsNothing,
    );

    for (final invalidValue in ['0', '-1', '1.5']) {
      await tester.enterText(field, invalidValue);
      await tester.pump();
      expect(
        find.byKey(const Key('purchase-submit-quantity-change')),
        findsNothing,
        reason: '$invalidValue must not produce a replacement request',
      );
    }

    await tester.enterText(field, '3');
    await tester.pump();
    expect(
      find.byKey(const Key('purchase-submit-quantity-change')),
      findsOneWidget,
    );
  });

  testWidgets('batch purchase confirms exactly the selected operation IDs', (
    tester,
  ) async {
    List<String>? operationIds;
    String? editMessage;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-batch',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-a',
            action: 'complete_purchase',
            description: '衛生紙 +2 包',
            values: {
              'name': '衛生紙',
              'purchased_quantity': 2,
              'purchased_unit': '包',
            },
          ),
          OperationView(
            id: 'operation-b',
            action: 'complete_purchase',
            description: '洗衣精 +1 瓶',
            values: {
              'name': '洗衣精',
              'purchased_quantity': 1,
              'purchased_unit': '瓶',
            },
          ),
        ],
      ),
      onDecision: ({
        required approved,
        selectedOptionId,
        approvedOperationIds,
      }) => operationIds = approvedOperationIds,
      onEdit: (message) => editMessage = message,
    );

    expect(
      tester
          .widget<CheckboxListTile>(
            find.byKey(const Key('purchase-operation-operation-a')),
          )
          .value,
      isTrue,
    );
    expect(
      tester
          .widget<CheckboxListTile>(
            find.byKey(const Key('purchase-operation-operation-b')),
          )
          .value,
      isTrue,
    );
    expect(
      find.byKey(const Key('purchase-edit-quantity-operation-a')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('purchase-edit-quantity-operation-b')),
      findsOneWidget,
    );
    expect(find.text('operation-a'), findsNothing);
    expect(find.text('operation-b'), findsNothing);

    await tester.tap(
      find.byKey(const Key('purchase-edit-quantity-operation-b')),
    );
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('purchase-quantity-field-operation-b')),
      '2',
    );
    await tester.pump();
    await tester.tap(
      find.byKey(const Key('purchase-submit-quantity-change-operation-b')),
    );
    expect(editMessage, '洗衣精：數量改成 2');
    expect(find.text('衛生紙 2 包'), findsOneWidget);
    expect(find.text('洗衣精 1 瓶'), findsOneWidget);

    await tester.tap(
      find.descendant(
        of: find.byKey(const Key('purchase-operation-operation-b')),
        matching: find.byType(Checkbox),
      ),
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('approval-confirm')));
    expect(operationIds, ['operation-a']);

    await tester.tap(
      find.descendant(
        of: find.byKey(const Key('purchase-operation-operation-a')),
        matching: find.byType(Checkbox),
      ),
    );
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );
  });

  testWidgets('cancelling a batch purchase does not submit operation IDs', (
    tester,
  ) async {
    bool? cancellationWasApproved;
    List<String>? operationIds;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-cancel',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-a',
            action: 'complete_purchase',
            description: '衛生紙 +2 包',
            values: {
              'name': '衛生紙',
              'purchased_quantity': 2,
              'purchased_unit': '包',
            },
          ),
        ],
      ),
      onDecision:
          ({required approved, selectedOptionId, approvedOperationIds}) {
            cancellationWasApproved = approved;
            operationIds = approvedOperationIds;
          },
    );

    await tester.tap(find.byKey(const Key('approval-cancel')));
    expect(cancellationWasApproved, isFalse);
    expect(operationIds, isNull);
  });

  testWidgets('generic batch actions select exact backend operation IDs', (
    tester,
  ) async {
    bool? approvalDecision;
    List<String>? operationIds;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-generic-batch',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-shopping-1',
            action: 'add_to_shopping_list',
            description: '新增牛奶到購物清單',
          ),
          OperationView(
            id: 'operation-shopping-2',
            action: 'add_to_shopping_list',
            description: '新增白米到購物清單',
          ),
        ],
      ),
      onDecision:
          ({required approved, selectedOptionId, approvedOperationIds}) {
            approvalDecision = approved;
            operationIds = approvedOperationIds;
          },
    );

    for (final id in ['operation-shopping-1', 'operation-shopping-2']) {
      expect(
        tester
            .widget<CheckboxListTile>(
              find.byKey(Key('generic-batch-operation-$id')),
            )
            .value,
        isTrue,
      );
    }
    await tester.tap(find.byKey(const Key('approval-cancel')));
    expect(approvalDecision, isFalse);
    expect(operationIds, isNull);

    await tester.tap(
      find.descendant(
        of: find.byKey(
          const Key('generic-batch-operation-operation-shopping-2'),
        ),
        matching: find.byType(Checkbox),
      ),
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('approval-confirm')));
    expect(approvalDecision, isTrue);
    expect(operationIds, ['operation-shopping-1']);

    await tester.tap(
      find.descendant(
        of: find.byKey(
          const Key('generic-batch-operation-operation-shopping-1'),
        ),
        matching: find.byType(Checkbox),
      ),
    );
    await tester.pump();
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );
  });

  testWidgets('purchase options accept unlabeled values and return option ID', (
    tester,
  ) async {
    String? selectedOption;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-options-1',
        presentation: ApprovalPresentation.purchaseOptions,
        operations: [
          OperationView(
            id: 'operation-options-1',
            action: 'choose_purchase_option',
            description: '選購項目',
          ),
        ],
        options: [
          ApprovalOptionView(
            id: 'option-1',
            values: {'store': '便利商店', 'price': 109, 'internal_id': 'hidden'},
          ),
        ],
      ),
      onDecision: ({
        required approved,
        selectedOptionId,
        approvedOperationIds,
      }) => selectedOption = selectedOptionId,
    );

    expect(find.textContaining('便利商店'), findsOneWidget);
    expect(find.textContaining('109'), findsOneWidget);
    expect(find.text('option-1'), findsNothing);
    expect(find.text('hidden'), findsNothing);
    expect(find.text('前往購買'), findsNothing);
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );
    await tester.tap(find.byKey(const Key('purchase-option-option-1')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('approval-confirm')));
    expect(selectedOption, 'option-1');
  });

  testWidgets('purchase option opens product URL without selecting it', (
    tester,
  ) async {
    Uri? openedUrl;
    String? selectedOption;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-options-link',
        presentation: ApprovalPresentation.purchaseOptions,
        operations: [
          OperationView(
            id: 'operation-options-link',
            action: 'choose_purchase_option',
            description: '選購項目',
          ),
        ],
        options: [
          ApprovalOptionView(
            id: 'option-link',
            values: {
              'store': '線上商店',
              'estimated_price': 99,
              'source': '推薦',
              'product_url': 'https://shop.example.test/product/1',
            },
          ),
          ApprovalOptionView(
            id: 'option-malformed-link',
            values: {'product_url': 'not a URL'},
          ),
        ],
      ),
      onOpenProductUrl: (url) async => openedUrl = url,
      onDecision: ({
        required approved,
        selectedOptionId,
        approvedOperationIds,
      }) => selectedOption = selectedOptionId,
    );

    expect(find.textContaining('product_url'), findsNothing);
    expect(find.textContaining('shop.example.test'), findsNothing);
    expect(find.textContaining('線上商店'), findsOneWidget);
    expect(find.textContaining('99'), findsOneWidget);
    expect(find.textContaining('推薦'), findsOneWidget);
    expect(
      find.byKey(const Key('purchase-option-link-option-link')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('purchase-option-link-option-malformed-link')),
      findsNothing,
    );

    await tester.tap(find.byKey(const Key('purchase-option-link-option-link')));
    await tester.pump();

    expect(openedUrl, Uri.parse('https://shop.example.test/product/1'));
    expect(selectedOption, isNull);
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );
  });
}
