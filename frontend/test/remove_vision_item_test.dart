import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('remove review items without writes at $size', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      const catalog = DemoCatalog();
      final before = (await catalog.inventory()).map((e) => e.detail).toList();
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.visionDemo),
          catalog: catalog,
        ),
      );
      await tester.enterText(find.byType(TextField), '照片');
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      expect(find.text('辨識到 3 件物品'), findsOneWidget);
      final firstCard = find.byKey(const Key('vision-item-0'));
      final secondCard = find.byKey(const Key('vision-item-1'));
      final location = find.descendant(
        of: firstCard,
        matching: find.byIcon(Icons.location_on_outlined),
      );
      expect(location, findsOneWidget);
      expect(
        tester.getTopLeft(location).dx,
        greaterThan(tester.getTopLeft(find.byKey(const Key('quantity-0'))).dx),
      );
      final date = find.descendant(
        of: secondCard,
        matching: find.text('選擇日期（選填）'),
      );
      await tester.ensureVisible(date);
      await tester.pumpAndSettle();
      await tester.tap(date);
      await tester.pumpAndSettle();
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      final today = DateTime.now();
      final formatted =
          '${today.year.toString().padLeft(4, '0')}-${today.month.toString().padLeft(2, '0')}-${today.day.toString().padLeft(2, '0')}';
      expect(
        find.descendant(of: secondCard, matching: find.text(formatted)),
        findsOneWidget,
      );
      final initialHeight = tester
          .getSize(find.byKey(const Key('work-surface')))
          .height;
      final removeFirst = find.descendant(
        of: firstCard,
        matching: find.byTooltip('移除此項'),
      );
      await tester.scrollUntilVisible(
        firstCard,
        -100,
        scrollable: find
            .descendant(
              of: find.byKey(const Key('work-items')),
              matching: find.byType(Scrollable),
            )
            .first,
      );
      await tester.pumpAndSettle();
      await tester.tap(removeFirst);
      await tester.pumpAndSettle();
      expect(find.text('辨識到 2 件物品'), findsOneWidget);
      expect(find.text('衛生紙'), findsNothing);
      expect(find.text('洗衣精'), findsOneWidget);
      expect(find.text('咖啡'), findsOneWidget);
      expect(
        find.descendant(of: secondCard, matching: find.text(formatted)),
        findsOneWidget,
      );
      expect(
        tester.getSize(find.byKey(const Key('work-surface'))).height,
        lessThanOrEqualTo(initialHeight),
      );
      for (final id in [1, 2]) {
        final remove = find.descendant(
          of: find.byKey(Key('vision-item-$id')),
          matching: find.byTooltip('移除此項'),
        );
        await tester.ensureVisible(remove);
        await tester.pumpAndSettle();
        await tester.tap(remove);
        await tester.pumpAndSettle();
      }
      expect(find.text('辨識到 0 件物品'), findsOneWidget);
      expect(find.text('目前沒有待確認的物品。'), findsOneWidget);
      expect(find.text('確認全部入庫'), findsNothing);
      expect(find.byTooltip('關閉'), findsOneWidget);
      expect((await catalog.inventory()).map((e) => e.detail).toList(), before);
      expect(tester.takeException(), isNull);
    });
  }
}
