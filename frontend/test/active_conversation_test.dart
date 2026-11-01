import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class LocalAgent implements AgentClient {
  @override
  Future<AgentReply> send(String message) async =>
      AgentReply(message: '回答 $message');
  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  }) async => const AgentReply(message: '完成');
}

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('active conversation and clear at $size', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(agentClient: LocalAgent(), catalog: const DemoCatalog()),
      );
      expect(find.byTooltip('語音輸入'), findsOneWidget);
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      for (final message in ['第一則', '第二則', '第三則', '第四則']) {
        await tester.enterText(input, message);
        await tester.tap(find.text('送出'));
        await tester.pumpAndSettle();
      }
      expect(find.text('第四則'), findsOneWidget);
      expect(find.text('回答 第四則'), findsOneWidget);
      expect(find.byKey(const Key('speech')), findsOneWidget);
      await tester.tap(find.text('清除對話'));
      await tester.pumpAndSettle();
      expect(find.text('第四則'), findsNothing);
      expect(find.text('回答 第四則'), findsNothing);
      expect(input.hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
}
