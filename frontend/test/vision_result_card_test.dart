import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/agent_result_card.dart';

Widget visionCard(Map<String, dynamic> data) => MaterialApp(
  home: Scaffold(
    body: SingleChildScrollView(
      child: AgentResultCard(
        result: AgentResultView(
          kind: ResultKind.vision,
          summary: '辨識摘要',
          data: data,
        ),
      ),
    ),
  ),
);

void main() {
  testWidgets('renders new vision item fields and a unique household match', (
    tester,
  ) async {
    await tester.pumpWidget(
      visionCard({
        'image_ref': 'demo-image',
        'scene_type': 'pantry',
        'items': [
          {
            'name': '鮮奶',
            'seen_as': '牛奶盒',
            'quantity': 2,
            'unit': '瓶',
            'unit_confirmed': true,
            'expiry_date': '2026-12-01',
            'match': {
              'method': 'exact',
              'item_id': 'inventory-milk',
              'item_name': '家中鮮奶',
              'item_unit': '瓶',
            },
          },
        ],
      }),
    );

    final card = find.byKey(const Key('vision-result-card'));
    expect(card, findsOneWidget);
    expect(find.text('尚未加入庫存'), findsOneWidget);
    expect(find.text('僅辨識，尚未加入庫存'), findsNothing);
    expect(find.text('照片辨識'), findsNothing);
    expect(find.text('辨識摘要'), findsNothing);
    expect(
      find.descendant(
        of: card,
        matching: find.byKey(const Key('confirmation-border-top')),
      ),
      findsOneWidget,
    );
    final border = tester.widget<Image>(
      find.descendant(
        of: card,
        matching: find.descendant(
          of: find.byKey(const Key('confirmation-border-top')),
          matching: find.byType(Image),
        ),
      ),
    );
    expect(
      (border.image as AssetImage).assetName,
      'assets/images/ui/confirmation_border_horizontal.png',
    );
    expect(
      find.descendant(of: card, matching: find.byType(FilledButton)),
      findsNothing,
    );
    expect(find.text('鮮奶'), findsOneWidget);
    expect(find.text('2 瓶'), findsOneWidget);
    expect(find.text('期限：2026-12-01'), findsOneWidget);
    expect(find.text('家中品項：家中鮮奶'), findsOneWidget);
    expect(find.text('牛奶盒'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'uses seen_as safely for unresolved, ambiguous, and low-confidence items',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 568));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.pumpWidget(
        visionCard({
          'items': [
            {
              'seen_as': '咖啡',
              'quantity': 2.5,
              'unit': null,
              'unit_confirmed': false,
              'low_confidence': true,
              'match': {
                'method': 'ambiguous',
                'choices': [
                  {'item_id': 'coffee', 'item_name': '咖啡豆'},
                  {'item_id': 'tea', 'item_name': '紅茶包'},
                  {'item_id': 'bad'},
                  'invalid',
                ],
              },
            },
            {
              'name': '未知物',
              'match': {'method': 'none'},
            },
          ],
        }),
      );

      expect(find.text('咖啡'), findsOneWidget);
      expect(find.text('2.5（單位待確認）'), findsOneWidget);
      expect(find.text('可能符合：咖啡豆、紅茶包'), findsOneWidget);
      expect(find.text('辨識結果待確認'), findsOneWidget);
      expect(find.text('未知物'), findsOneWidget);
      expect(find.textContaining('家中品項：未知'), findsNothing);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('empty or malformed new-format items retain the summary', (
    tester,
  ) async {
    await tester.pumpWidget(
      visionCard({
        'items': [
          null,
          42,
          {'quantity': []},
          {
            'name': '茶',
            'quantity': 'invalid',
            'unit': '包',
            'unit_confirmed': 'true',
            'match': 'invalid',
          },
        ],
      }),
    );

    expect(find.text('辨識摘要'), findsNothing);
    expect(find.text('尚未加入庫存'), findsOneWidget);
    expect(find.text('茶'), findsOneWidget);
    expect(find.text('單位：包（待確認）'), findsOneWidget);
    expect(find.textContaining('invalid'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('empty items keep the summary and read-only warning', (
    tester,
  ) async {
    await tester.pumpWidget(visionCard({'items': []}));

    expect(find.text('辨識摘要'), findsNothing);
    expect(find.text('尚未加入庫存'), findsOneWidget);
  });

  testWidgets('non-vision results keep the existing generic card renderer', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AgentResultCard(
            result: const AgentResultView(
              kind: ResultKind.inventory,
              summary: '庫存查詢摘要',
              data: {'items': []},
            ),
          ),
        ),
      ),
    );

    expect(find.byType(Card), findsOneWidget);
    expect(find.text('庫存'), findsOneWidget);
    expect(find.text('庫存查詢摘要'), findsOneWidget);
    expect(find.byKey(const Key('vision-result-card')), findsNothing);
  });
}
