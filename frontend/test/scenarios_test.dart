import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';

import 'fake_camera.dart';

import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  final cases = <MockScenario, String>{
    MockScenario.completed: '示範庫存：白米 2 包。',
    MockScenario.singleAction: '單一操作確認示範',
    MockScenario.batchAction: '批次操作確認示範',
    MockScenario.purchaseOptions: '選購項目確認示範',
    MockScenario.partialSuccess: '部分成功（示範）：沒有儲存任何變更。',
    MockScenario.error: '暫時無法完成，請重試。',
    MockScenario.timeout: '等待逾時，請重試。',
  };
  for (final entry in cases.entries) {
    testWidgets('scenario ${entry.key.name}', (tester) async {
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: entry.key),
          catalog: const DemoCatalog(),
        ),
      );
      await tester.enterText(find.byType(TextField), '示範');
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      expect(find.text(entry.value), findsOneWidget);
      if ([
        MockScenario.singleAction,
        MockScenario.batchAction,
        MockScenario.purchaseOptions,
      ].contains(entry.key)) {
        expect(find.textContaining('新增牛奶'), findsOneWidget);
        if (entry.key != MockScenario.singleAction) {
          expect(find.textContaining('新增白米'), findsOneWidget);
        }
        await tester.ensureVisible(find.text('確認示範'));
        await tester.pumpAndSettle();
        await tester.tap(find.text('確認示範'));
        await tester.pumpAndSettle();
        expect(find.text('確認流程示範結束，沒有儲存任何變更。'), findsOneWidget);
      }
    });
  }
  testWidgets('camera capture failure is explained', (tester) async {
    final camera = FakeCamera()..fail = true;
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(),
        catalog: const DemoCatalog(),
        cameraCapture: camera,
      ),
    );
    await tester.tap(find.text('拍照'));
    await tester.pumpAndSettle();
    expect(find.text('無法開啟相機，請稍後再試。'), findsOneWidget);
  });
}
