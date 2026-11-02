import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('local edits remain associated after removal at $size', (
      tester,
    ) async {
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
      await tester.enterText(find.byType(TextField), '/vision-demo');
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      final q = find.byKey(const Key('quantity-0'));
      final loc = find.byKey(const Key('location-0'));
      await tester.ensureVisible(q);
      await tester.pumpAndSettle();
      await tester.enterText(q, '5');
      await tester.enterText(loc, '客廳櫃');
      await tester.pumpAndSettle();
      expect(tester.widget<TextField>(q).controller!.text, '5');
      await tester.enterText(q, '-2');
      expect(tester.widget<TextField>(q).controller!.text, '5');
      await tester.enterText(q, 'abc');
      expect(tester.widget<TextField>(q).controller!.text, '5');
      await tester.enterText(q, '0');
      await tester.pumpAndSettle();
      expect(find.text('請輸入大於零的數量'), findsOneWidget);
      expect(
        tester
            .widget<FilledButton>(find.widgetWithText(FilledButton, '確認全部入庫'))
            .onPressed,
        isNull,
      );
      await tester.enterText(q, '5');
      await tester.pumpAndSettle();
      final second = find.byKey(const Key('vision-item-1'));
      final date = find.descendant(of: second, matching: find.text('選擇日期（選填）'));
      await tester.ensureVisible(date);
      await tester.pumpAndSettle();
      await tester.tap(date);
      await tester.pumpAndSettle();
      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      final now = DateTime.now();
      final value =
          '${now.year.toString().padLeft(4, '0')}-${now.month.toString().padLeft(2, '0')}-${now.day.toString().padLeft(2, '0')}';
      final third = find.byKey(const Key('vision-item-2'));
      final remove = find.descendant(
        of: third,
        matching: find.byTooltip('移除此項'),
      );
      await tester.ensureVisible(remove);
      await tester.pumpAndSettle();
      await tester.tap(remove);
      await tester.pumpAndSettle();
      expect(find.text('辨識到 2 件物品'), findsOneWidget);
      expect(tester.widget<TextField>(q).controller!.text, '5');
      expect(tester.widget<TextField>(loc).controller!.text, '客廳櫃');
      expect(
        find.descendant(of: second, matching: find.text(value)),
        findsOneWidget,
      );
      expect(
        find.descendant(
          of: find.byKey(const Key('vision-item-0')),
          matching: find.text('包'),
        ),
        findsOneWidget,
      );
      await tester.tap(find.text('確認全部入庫'));
      await tester.pumpAndSettle();
      expect((await catalog.inventory()).map((e) => e.detail).toList(), before);
      expect(tester.takeException(), isNull);
    });
  }
}
