import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('scene and input remain usable at $size', (tester) async {
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
      expect(find.byKey(const Key('room')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.ensureVisible(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.text('白米'), findsOneWidget);
      if (find.byTooltip('關閉庫存').evaluate().isNotEmpty) {
        await tester.tap(find.byTooltip('關閉庫存'));
      } else {
        await tester.pageBack();
      }
      await tester.pumpAndSettle();
      await tester.ensureVisible(find.byType(TextField));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), '查看庫存');
      await tester.ensureVisible(find.text('送出'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      await tester.scrollUntilVisible(
        find.text('單一操作確認示範'),
        200,
        scrollable: find.byType(Scrollable).first,
      );
      expect(find.text('單一操作確認示範'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
}
