import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/presentation/approval_card.dart';
import 'package:nestock/features/agent/presentation/approval_card_config.dart';

void main() {
  Future<void> pumpCard(
    WidgetTester tester,
    ApprovalView approval, {
    ApprovalDecisionCallback? onDecision,
    ValueChanged<String>? onEdit,
    ValueChanged<List<CardChange>>? onCardChanges,
    Future<void> Function(Uri url)? onOpenProductUrl,
    ApprovalCardConfig config = const ApprovalCardConfig(),
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
            onSubmitCardChanges: onCardChanges,
            onOpenProductUrl: onOpenProductUrl,
            config: config,
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
      expect(find.byKey(const Key('confirmation-name-0')), findsNothing);
      expect(find.byKey(const Key('confirmation-location-0')), findsNothing);

      await tester.tap(find.byKey(const Key('approval-option-new_item')));

      expect(didApprove, isTrue);
      expect(selectedOption, 'new_item');
    },
  );

  testWidgets(
    'photo create item submits structured name quantity and location changes',
    (tester) async {
      List<CardChange>? changes;
      bool? decision;
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
        onCardChanges: (value) => changes = value,
        onDecision: ({
          required approved,
          selectedOptionId,
          approvedOperationIds,
        }) => decision = approved,
      );

      expect(find.text('確認建立庫存品項'), findsOneWidget);
      expect(find.text('會新增一個庫存品項：洗碗精'), findsOneWidget);
      expect(find.text('洗碗精'), findsOneWidget);
      await tester.tap(find.byKey(const Key('confirmation-edit-0')));
      await tester.pump();
      expect(find.byKey(const Key('confirmation-name-0')), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('confirmation-quantity-0')),
        '3',
      );
      await tester.enterText(
        find.byKey(const Key('confirmation-name-0')),
        '洗手乳',
      );
      await tester.enterText(
        find.byKey(const Key('confirmation-location-0')),
        '浴室櫃',
      );
      await tester.pump();
      expect(find.byKey(const Key('confirmation-unit-0')), findsNothing);
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('confirmation-confirm')))
            .onPressed,
        isNull,
      );
      await tester.tap(find.byKey(const Key('confirmation-cancel')));
      expect(decision, isFalse);
      await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

      expect(changes!.single.toJson(), {
        'operation_id': 'operation-create-1',
        'name': '洗手乳',
        'quantity': 3,
        'location': '浴室櫃',
      });
    },
  );

  testWidgets(
    'existing inventory increase uses a concise renderer for photo and spoken approvals',
    (tester) async {
      final decisions = <bool>[];
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-existing-increase',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-existing-increase',
              action: increaseStockApprovalAction,
              description: '滑鼠 +1 個（家裡已有的品項）',
              values: {
                'item_id': 'inventory-mouse',
                'name': '滑鼠',
                'quantity_added': 1,
                'unit': '個',
                'transaction_id': 'transaction-mouse',
                'reason': 'adjustment',
                'inventory_quantity_before': 1,
              },
            ),
          ],
        ),
        onDecision: ({
          required approved,
          selectedOptionId,
          approvedOperationIds,
        }) => decisions.add(approved),
      );

      expect(
        find.byKey(const Key('increase-stock-approval-card')),
        findsOneWidget,
      );
      expect(find.text('確認增加庫存'), findsOneWidget);
      expect(find.text('滑鼠'), findsOneWidget);
      expect(find.text('增加 1 個'), findsOneWidget);
      expect(find.text('家裡已有的品項'), findsOneWidget);
      expect(find.textContaining('item_id'), findsNothing);
      expect(find.textContaining('transaction_id'), findsNothing);
      expect(find.textContaining('reason'), findsNothing);
      expect(find.textContaining('inventory_quantity_before'), findsNothing);
      expect(
        tester
            .widget<TextButton>(find.byKey(const Key('approval-cancel')))
            .onPressed,
        isNotNull,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
            .onPressed,
        isNotNull,
      );

      await tester.tap(find.byKey(const Key('approval-cancel')));
      await tester.tap(find.byKey(const Key('approval-confirm')));
      expect(decisions, [false, true]);
    },
  );

  testWidgets(
    'malformed existing inventory increase disables confirmation without exposing metadata',
    (tester) async {
      final decisions = <bool>[];
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-malformed-increase',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-malformed-increase',
              action: increaseStockApprovalAction,
              description: '內部增加庫存資料',
              values: {
                'item_id': 'inventory-mouse',
                'transaction_id': 'transaction-mouse',
                'reason': 'adjustment',
                'inventory_quantity_before': 1,
              },
            ),
          ],
        ),
        onDecision: ({
          required approved,
          selectedOptionId,
          approvedOperationIds,
        }) => decisions.add(approved),
      );

      expect(
        find.byKey(const Key('increase-stock-approval-card')),
        findsOneWidget,
      );
      expect(find.text('確認增加庫存'), findsOneWidget);
      expect(find.text('這筆增加庫存資料不完整，請取消後重新確認。'), findsOneWidget);
      expect(find.textContaining('item_id'), findsNothing);
      expect(find.textContaining('transaction_id'), findsNothing);
      expect(find.textContaining('reason'), findsNothing);
      expect(find.textContaining('inventory_quantity_before'), findsNothing);
      expect(
        tester
            .widget<TextButton>(find.byKey(const Key('approval-cancel')))
            .onPressed,
        isNotNull,
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
            .onPressed,
        isNull,
      );

      await tester.tap(find.byKey(const Key('approval-cancel')));
      await tester.tap(find.byKey(const Key('approval-confirm')));
      expect(decisions, [false]);
    },
  );

  testWidgets('mixed create and increase batch remains a generic batch', (
    tester,
  ) async {
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-mixed-inventory-batch',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-create',
            action: 'create_item',
            description: '新增庫存品項：新滑鼠',
            values: {'name': '新滑鼠', 'quantity': 1, 'unit': '個'},
          ),
          OperationView(
            id: 'operation-increase',
            action: 'increase_stock',
            description: '既有滑鼠增加 1 個',
            values: {'name': '既有滑鼠', 'quantity_added': 1, 'unit': '個'},
          ),
        ],
      ),
    );

    expect(find.byKey(const Key('increase-stock-approval-card')), findsNothing);
    expect(
      find.byKey(const Key('generic-batch-operation-operation-create')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('generic-batch-operation-operation-increase')),
      findsOneWidget,
    );
  });

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

  testWidgets('create item can clear a location with an explicit null', (
    tester,
  ) async {
    List<CardChange>? changes;
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
      onCardChanges: (value) => changes = value,
    );

    expect(find.text('書桌抽屜'), findsOneWidget);
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('confirmation-location-0')),
      '',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

    expect(changes!.single.toJson(), {
      'operation_id': 'operation-create-valid-location',
      'location': null,
    });
  });

  testWidgets('blank optional location stays null in an edit request', (
    tester,
  ) async {
    List<CardChange>? changes;
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
      onCardChanges: (value) => changes = value,
    );

    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('confirmation-quantity-0')),
      '2',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

    expect(changes!.single.toJson(), {
      'operation_id': 'operation-create-blank-edit',
      'quantity': 2,
    });
  });

  testWidgets('said new-item card exposes name and location editing', (
    tester,
  ) async {
    List<CardChange>? changes;
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-said-new-item',
        presentation: ApprovalPresentation.singleAction,
        operations: [
          OperationView(
            id: 'operation-said-new-item',
            action: 'create_item',
            description: '會新增一個庫存品項：洗髮精（使用者說的）',
            values: {
              'name': '洗髮精',
              'quantity': 1,
              'unit': '瓶',
              'location': null,
            },
          ),
        ],
      ),
      onCardChanges: (value) => changes = value,
    );

    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    expect(find.byKey(const Key('confirmation-name-0')), findsOneWidget);
    expect(find.byKey(const Key('confirmation-location-0')), findsOneWidget);
    await tester.enterText(
      find.byKey(const Key('confirmation-location-0')),
      '浴室',
    );
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-submit-changes')));

    expect(changes!.single.toJson(), {
      'operation_id': 'operation-said-new-item',
      'location': '浴室',
    });
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
        find.byKey(const Key('create-item-batch-approval-card')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('purchase-approval-card')), findsNothing);
      expect(find.text('滑鼠 1 個'), findsOneWidget);
      expect(find.text('鍵盤 1 個'), findsOneWidget);
      expect(find.textContaining('opaque-'), findsNothing);

      await tester.tap(
        find.byKey(
          const Key('create-item-operation-operation-create-keyboard'),
        ),
      );
      await tester.tap(find.byKey(const Key('approval-confirm')));

      expect(selectedOperationIds, ['operation-create-mouse']);
    },
  );

  testWidgets('batch create item draft blocks confirmation', (tester) async {
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-create-batch-draft',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-create-draft',
            action: 'create_item',
            description: '建立庫存品項：滑鼠',
            values: {'name': '滑鼠', 'quantity': 1, 'unit': '個'},
          ),
        ],
      ),
      onCardChanges: (_) {},
    );

    await tester.tap(
      find.byKey(const Key('create-item-edit-quantity-operation-create-draft')),
    );
    await tester.pump();
    await tester.enterText(
      find.byKey(const Key('create-item-quantity-operation-create-draft')),
      '2',
    );
    await tester.pump();

    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );
    expect(
      tester
          .widget<TextButton>(find.byKey(const Key('approval-cancel')))
          .onPressed,
      isNotNull,
    );
  });

  testWidgets(
    'batch photo create items submit only changed name quantity and location',
    (tester) async {
      List<CardChange>? changes;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-create-batch-edit',
          presentation: ApprovalPresentation.batchAction,
          operations: [
            OperationView(
              id: 'operation-create-batch-edit',
              action: 'create_item',
              description: '建立庫存品項：滑鼠',
              values: {
                'name': '滑鼠',
                'quantity': 1,
                'unit': '個',
                'location': null,
              },
            ),
          ],
        ),
        onCardChanges: (value) => changes = value,
      );

      await tester.tap(
        find.byKey(
          const Key('create-item-edit-quantity-operation-create-batch-edit'),
        ),
      );
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('create-item-name-operation-create-batch-edit')),
        '無線滑鼠',
      );
      await tester.enterText(
        find.byKey(
          const Key('create-item-quantity-operation-create-batch-edit'),
        ),
        '2',
      );
      await tester.enterText(
        find.byKey(
          const Key('create-item-location-operation-create-batch-edit'),
        ),
        '書桌抽屜',
      );
      await tester.pump();
      await tester.tap(
        find.byKey(const Key('create-item-submit-card-changes')),
      );

      expect(changes!.single.toJson(), {
        'operation_id': 'operation-create-batch-edit',
        'name': '無線滑鼠',
        'quantity': 2,
        'location': '書桌抽屜',
      });
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
      List<CardChange>? changes;
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
        onCardChanges: (value) => changes = value,
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              approvalWasAccepted = approved;
              operationIds = approvedOperationIds;
            },
      );

      expect(find.byKey(const Key('purchase-approval-card')), findsOneWidget);
      expect(
        find.descendant(
          of: find.byKey(const Key('purchase-approval-card')),
          matching: find.byKey(const Key('confirmation-border-top')),
        ),
        findsOneWidget,
      );
      expect(find.text('衛生紙 2 包'), findsOneWidget);
      expect(find.textContaining('購買日期：'), findsNothing);
      expect(find.text('shopping-1'), findsNothing);
      expect(find.text('item-1'), findsNothing);
      expect(find.text('transaction-1'), findsNothing);
      expect(find.text('位置'), findsNothing);
      expect(find.byKey(const Key('confirmation-name-0')), findsNothing);
      expect(find.byKey(const Key('confirmation-unit-0')), findsNothing);
      expect(find.byKey(const Key('confirmation-location-0')), findsNothing);
      await tester.tap(find.byKey(const Key('purchase-edit-quantity')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('purchase-quantity-field')),
        '3',
      );
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
            .onPressed,
        isNull,
      );
      await tester.tap(find.byKey(const Key('approval-cancel')));
      expect(approvalWasAccepted, isFalse);
      await tester.tap(find.byKey(const Key('purchase-submit-card-changes')));
      expect(changes!.single.toJson(), {
        'operation_id': 'operation-purchase-1',
        'quantity': 3,
      });

      expect(operationIds, isNull);
    },
  );

  testWidgets(
    'Shopping-backed create item uses the purchase card and structured edits',
    (tester) async {
      List<CardChange>? changes;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-shopping-create-purchase',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-shopping-create-purchase',
              action: 'create_item',
              description: '建立庫存品項：衛生紙',
              values: {
                'name': '衛生紙',
                'quantity': 2,
                'unit': '包',
                'location': '浴室櫃',
                'shopping_item_id': 'shopping-first-purchase',
                'transaction_id': 'transaction-first-purchase',
                'purchased_at': '2026-11-09T12:00:00+08:00',
              },
            ),
          ],
        ),
        onCardChanges: (value) => changes = value,
      );

      expect(find.byKey(const Key('purchase-approval-card')), findsOneWidget);
      expect(find.byKey(const Key('confirmation-card-shell')), findsNothing);
      expect(find.text('衛生紙 2 包'), findsOneWidget);
      expect(find.text('購買日期：2026/11/09'), findsOneWidget);
      expect(find.textContaining('shopping-first-purchase'), findsNothing);
      expect(find.textContaining('transaction-first-purchase'), findsNothing);

      await tester.tap(find.byKey(const Key('purchase-edit-quantity')));
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('purchase-quantity-field')),
        '3',
      );
      await tester.pump();

      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
            .onPressed,
        isNull,
      );
      await tester.tap(find.byKey(const Key('purchase-submit-card-changes')));
      expect(changes!.single.toJson(), {
        'operation_id': 'operation-shopping-create-purchase',
        'quantity': 3,
      });
    },
  );

  testWidgets(
    'mixed complete purchase and Shopping-backed create items use one purchase card',
    (tester) async {
      List<String>? selectedIds;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-mixed-shopping-purchase',
          presentation: ApprovalPresentation.batchAction,
          operations: [
            OperationView(
              id: 'operation-existing-item',
              action: 'complete_purchase',
              description: '洗衣精 +1 瓶',
              values: {
                'name': '洗衣精',
                'purchased_quantity': 1,
                'purchased_unit': '瓶',
              },
            ),
            OperationView(
              id: 'operation-first-item',
              action: 'create_item',
              description: '建立庫存品項：衛生紙',
              values: {
                'name': '衛生紙',
                'quantity': 2,
                'unit': '包',
                'location': '浴室櫃',
                'shopping_item_id': 'shopping-first-item',
                'transaction_id': 'transaction-first-item',
                'purchased_at': '2026-11-09T12:00:00+08:00',
              },
            ),
          ],
        ),
        onDecision:
            ({required approved, selectedOptionId, approvedOperationIds}) {
              selectedIds = approvedOperationIds;
            },
      );

      expect(find.byKey(const Key('purchase-approval-card')), findsOneWidget);
      expect(
        find.byKey(const Key('generic-batch-approval-card')),
        findsNothing,
      );
      expect(
        find.byKey(const Key('create-item-batch-approval-card')),
        findsNothing,
      );
      expect(find.text('洗衣精 1 瓶'), findsOneWidget);
      expect(find.text('衛生紙 2 包'), findsOneWidget);
      expect(find.text('購買日期：2026/11/09'), findsOneWidget);

      await tester.tap(
        find.descendant(
          of: find.byKey(const Key('purchase-operation-operation-first-item')),
          matching: find.byType(Checkbox),
        ),
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('approval-confirm')));

      expect(selectedIds, ['operation-existing-item']);
    },
  );

  testWidgets(
    'photo create item mixed with a purchase keeps the generic batch fallback',
    (tester) async {
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-photo-and-purchase',
          presentation: ApprovalPresentation.batchAction,
          operations: [
            OperationView(
              id: 'operation-photo-item',
              action: 'create_item',
              description: '建立庫存品項：滑鼠',
              values: {'name': '滑鼠', 'quantity': 1, 'unit': '個'},
            ),
            OperationView(
              id: 'operation-purchased-item',
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

      expect(
        find.byKey(const Key('generic-batch-approval-card')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('purchase-approval-card')), findsNothing);
      expect(
        find.byKey(const Key('create-item-batch-approval-card')),
        findsNothing,
      );
    },
  );

  testWidgets('purchase dates retain each supplied timestamp calendar day', (
    tester,
  ) async {
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-dates',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-east',
            action: 'complete_purchase',
            description: '衛生紙 +2 包',
            values: {
              'name': '衛生紙',
              'purchased_quantity': 2,
              'purchased_unit': '包',
              'purchased_at': '2026-11-09T00:30:00+14:00',
            },
          ),
          OperationView(
            id: 'operation-west',
            action: 'complete_purchase',
            description: '洗衣精 +1 瓶',
            values: {
              'name': '洗衣精',
              'purchased_quantity': 1,
              'purchased_unit': '瓶',
              'purchased_at': '2026-11-09T23:30:00-11:00',
            },
          ),
        ],
      ),
    );

    expect(find.text('購買日期：2026/11/08'), findsOneWidget);
    expect(find.text('購買日期：2026/11/10'), findsOneWidget);
    expect(
      find.byKey(const Key('purchase-date-operation-east')),
      findsOneWidget,
    );
    expect(
      find.byKey(const Key('purchase-date-operation-west')),
      findsOneWidget,
    );
    expect(find.textContaining('2026-11-09T'), findsNothing);
    expect(find.byTooltip('修改購買日期'), findsNothing);
    expect(find.text('補填購買日期'), findsNothing);
    expect(find.text('修改購買日期'), findsNothing);
    expect(find.text('改回今天'), findsNothing);
  });

  testWidgets('purchase card omits missing and invalid purchase dates', (
    tester,
  ) async {
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-invalid-date',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-missing-date',
            action: 'complete_purchase',
            description: '衛生紙 +2 包',
            values: {
              'name': '衛生紙',
              'purchased_quantity': 2,
              'purchased_unit': '包',
              'created_at': '2026-11-11T12:00:00+08:00',
            },
          ),
          OperationView(
            id: 'operation-invalid-date',
            action: 'complete_purchase',
            description: '洗衣精 +1 瓶',
            values: {
              'name': '洗衣精',
              'purchased_quantity': 1,
              'purchased_unit': '瓶',
              'purchased_at': '2026-02-29T12:00:00+08:00',
            },
          ),
        ],
      ),
    );

    expect(find.textContaining('購買日期：'), findsNothing);
    expect(find.textContaining('purchased_at'), findsNothing);
    expect(find.textContaining('2026-02-29'), findsNothing);
  });

  testWidgets(
    'purchase date reset sends an explicit null, not a text command',
    (tester) async {
      List<CardChange>? changes;
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-purchase-reset-date',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(
              id: 'operation-reset-date',
              action: 'complete_purchase',
              description: '衛生紙 +2 包',
              values: {
                'name': '衛生紙',
                'purchased_quantity': 2,
                'purchased_unit': '包',
                'purchased_at': '2026-11-09T12:00:00+08:00',
              },
            ),
          ],
        ),
        onCardChanges: (value) => changes = value,
        config: const ApprovalCardConfig(datedPurchasesEnabled: true),
      );

      await tester.tap(find.byKey(const Key('purchase-edit-quantity')));
      await tester.pump();
      expect(find.text('修改購買日期'), findsOneWidget);
      expect(find.text('改回今天'), findsOneWidget);
      await tester.tap(
        find.byKey(const Key('purchase-reset-date-operation-reset-date')),
      );
      await tester.pump();
      await tester.tap(find.byKey(const Key('purchase-submit-card-changes')));

      expect(changes!.single.toJson(), {
        'operation_id': 'operation-reset-date',
        'purchased_on': null,
      });
    },
  );

  testWidgets('purchase dates fit a narrow Android viewport without overflow', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(320, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await pumpCard(
      tester,
      const ApprovalView(
        id: 'approval-purchase-narrow-date',
        presentation: ApprovalPresentation.batchAction,
        operations: [
          OperationView(
            id: 'operation-long-a',
            action: 'complete_purchase',
            description: '長名稱衛生紙 +2 包',
            values: {
              'name': '特長名稱的衛生紙補貨項目',
              'purchased_quantity': 2,
              'purchased_unit': '包',
              'purchased_at': '2026-11-09T12:00:00+08:00',
            },
          ),
          OperationView(
            id: 'operation-long-b',
            action: 'complete_purchase',
            description: '長名稱洗衣精 +1 瓶',
            values: {
              'name': '特長名稱的洗衣精補貨項目',
              'purchased_quantity': 1,
              'purchased_unit': '瓶',
              'purchased_at': '2026-11-10T12:00:00+08:00',
            },
          ),
        ],
      ),
    );

    expect(tester.takeException(), isNull);
    expect(find.text('購買日期：2026/11/09'), findsOneWidget);
    expect(find.text('購買日期：2026/11/10'), findsOneWidget);
  });

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
    expect(find.byKey(const Key('purchase-submit-card-changes')), findsNothing);

    for (final invalidValue in ['0', '-1', '1.5']) {
      await tester.enterText(field, invalidValue);
      await tester.pump();
      expect(
        find.byKey(const Key('purchase-submit-card-changes')),
        findsNothing,
        reason: '$invalidValue must not produce a replacement request',
      );
      expect(
        tester
            .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
            .onPressed,
        isNull,
      );
    }

    await tester.enterText(field, '3');
    await tester.pump();
    expect(
      find.byKey(const Key('purchase-submit-card-changes')),
      findsOneWidget,
    );
  });

  testWidgets('batch purchase confirms exactly the selected operation IDs', (
    tester,
  ) async {
    List<String>? operationIds;
    List<CardChange>? changes;
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
      onCardChanges: (value) => changes = value,
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
    await tester.tap(find.byKey(const Key('purchase-submit-card-changes')));
    expect(changes!.single.toJson(), {
      'operation_id': 'operation-b',
      'quantity': 2,
    });
    expect(find.text('衛生紙 2 包'), findsOneWidget);
    expect(find.text('洗衣精 1 瓶'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(find.byKey(const Key('approval-confirm')))
          .onPressed,
      isNull,
    );

    await tester.tap(
      find.descendant(
        of: find.byKey(const Key('purchase-operation-operation-b')),
        matching: find.byType(Checkbox),
      ),
    );
    await tester.pump();
    expect(operationIds, isNull);
    expect(
      tester
          .widget<CheckboxListTile>(
            find.byKey(const Key('purchase-operation-operation-b')),
          )
          .value,
      isFalse,
    );

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

  testWidgets(
    'batch purchase blocks all changes while an edited row is invalid',
    (tester) async {
      await pumpCard(
        tester,
        const ApprovalView(
          id: 'approval-purchase-batch-invalid',
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
        onCardChanges: (_) {},
      );

      await tester.tap(
        find.byKey(const Key('purchase-edit-quantity-operation-a')),
      );
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('purchase-quantity-field-operation-a')),
        '3',
      );
      await tester.tap(
        find.byKey(const Key('purchase-edit-quantity-operation-b')),
      );
      await tester.pump();
      await tester.enterText(
        find.byKey(const Key('purchase-quantity-field-operation-b')),
        '0',
      );
      await tester.pump();

      expect(
        find.byKey(const Key('purchase-submit-card-changes')),
        findsNothing,
      );
    },
  );

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
