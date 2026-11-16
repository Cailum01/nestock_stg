import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'agent_fixtures.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('vision work surface opens and closes at $size', (
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
          agentClient: FixtureAgentClient(visionFixtureReply()),
          catalog: catalog,
        ),
      );
      expect(find.byKey(const Key('work-surface')), findsNothing);
      await tester.enterText(find.byType(TextField), '查看照片');
      await tester.tap(find.byKey(const Key('paw-send')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-surface')), findsOneWidget);
      for (final entry in [(0, '衛生紙'), (1, '洗衣精'), (2, '咖啡')]) {
        await tester.scrollUntilVisible(
          find.byKey(Key('vision-item-${entry.$1}')),
          100,
          scrollable: find
              .descendant(
                of: find.byKey(const Key('work-items')),
                matching: find.byType(Scrollable),
              )
              .first,
        );
        await tester.pumpAndSettle();
        final card = find.byKey(Key('vision-item-${entry.$1}'));
        expect(
          find.descendant(of: card, matching: find.text(entry.$2)),
          findsOneWidget,
        );
        expect(
          find.descendant(of: card, matching: find.text('效期')),
          findsOneWidget,
        );
        expect(
          find.descendant(of: card, matching: find.text('選擇日期（選填）')),
          findsOneWidget,
        );
      }
      final panel = tester.getRect(find.byKey(const Key('work-surface')));
      final room = tester.getRect(find.byKey(const Key('room')));
      expect(panel.bottom, lessThanOrEqualTo(room.bottom));
      expect(panel.top, greaterThan(room.top));
      expect(
        find
            .descendant(
              of: find.byKey(const Key('composer')),
              matching: find.byType(TextField),
            )
            .hitTestable(),
        findsOneWidget,
      );
      expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
      await tester.tap(find.text('確認全部入庫'));
      await tester.pumpAndSettle();
      expect((await catalog.inventory()).map((e) => e.detail).toList(), before);
      await tester.tap(find.byTooltip('關閉'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets('large fixture scrolls within the panel', (tester) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: FixtureAgentClient(visionFixtureReply(many: true)),
        catalog: const DemoCatalog(),
      ),
    );
    await tester.enterText(find.byType(TextField), '查看照片');
    await tester.tap(find.byKey(const Key('paw-send')));
    await tester.pumpAndSettle();
    await tester.drag(
      find.byKey(const Key('work-items')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.text('麵'),
      300,
      scrollable: find
          .descendant(
            of: find.byKey(const Key('work-items')),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    await tester.pumpAndSettle();
    expect(find.text('麵'), findsOneWidget);
    expect(find.text('確認全部入庫').hitTestable(), findsOneWidget);
  });
}
