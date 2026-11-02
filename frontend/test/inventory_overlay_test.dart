import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('inventory overlay opens and searches at $size', (
      tester,
    ) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsOneWidget);
      expect(find.byKey(const Key('room')), findsOneWidget);
      expect(find.byKey(const Key('composer')).hitTestable(), findsOneWidget);
      expect(find.byKey(const Key('toolbar')).hitTestable(), findsOneWidget);
      await tester.enterText(
        find.byKey(const Key('inventory-search')),
        'TOILET PAPER',
      );
      await tester.pumpAndSettle();
      expect(find.text('衛生紙'), findsOneWidget);
      expect(find.text('顯示 1 件'), findsOneWidget);
      await tester.enterText(find.byKey(const Key('inventory-search')), '不存在');
      await tester.pumpAndSettle();
      expect(find.text('找不到符合的物品。'), findsOneWidget);
      await tester.tap(find.byTooltip('關閉庫存'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('庫存'));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
}
