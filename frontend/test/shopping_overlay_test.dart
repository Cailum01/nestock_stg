import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('shopping and inventory coordinate at $size', (tester) async {
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
      await tester.tap(find.byKey(const Key('cart')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
      expect(find.text('牛奶'), findsOneWidget);
      expect(find.byKey(const Key('composer')), findsOneWidget);
      expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(find.text('我的庫存'), findsOneWidget);
      await tester.tap(find.byTooltip('關閉庫存'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cart')));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
}
