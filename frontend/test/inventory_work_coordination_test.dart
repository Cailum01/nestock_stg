import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  testWidgets('inventory hides work surface without losing local edits', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(scenario: MockScenario.visionDemo),
        catalog: const DemoCatalog(),
      ),
    );
    await tester.enterText(find.byType(TextField), '照片');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('quantity-0')), '5');
    await tester.pumpAndSettle();
    await tester.tap(find.text('庫存'));
    await tester.pumpAndSettle();
    expect(find.text('我的庫存'), findsOneWidget);
    await tester.tap(find.byTooltip('關閉庫存'));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('quantity-0')))
          .controller!
          .text,
      '5',
    );
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsOneWidget);
  });
}
