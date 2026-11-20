import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class LocalAgent implements AgentClient {
  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async =>
      AgentReply(message: '回答 $message');
  @override
  Future<AgentReply> submitCardChanges({
    required String approvalId,
    required List<CardChange> changes,
  }) async => const AgentReply(message: '完成');
  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => const AgentReply(message: '完成');
}

void main() {
  testWidgets('conversation action toggles expanded interaction in place', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(agentClient: LocalAgent(), catalog: const DemoCatalog()),
    );

    final expand = find.byKey(const Key('paw-expand-interaction'));
    final cluster = find.byKey(const Key('paw-action-cluster'));
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    final initialActionRect = tester.getRect(expand);
    final initialClusterRect = tester.getRect(cluster);

    expect(find.byTooltip('展開對話'), findsOneWidget);
    expect(
      find.descendant(of: expand, matching: find.byIcon(Icons.expand_less)),
      findsOneWidget,
    );
    expect(find.byKey(const Key('work-surface')), findsNothing);
    await tester.tap(expand);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')), findsNothing);

    await tester.enterText(input, '查看對話');
    await tester.tap(find.byKey(const Key('paw-send')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsOneWidget);
    expect(find.byTooltip('收合對話'), findsOneWidget);
    expect(
      find.descendant(of: expand, matching: find.byIcon(Icons.expand_more)),
      findsOneWidget,
    );

    await tester.tap(expand);
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);
    expect(find.byTooltip('展開對話'), findsOneWidget);
    expect(
      find.descendant(of: expand, matching: find.byIcon(Icons.expand_less)),
      findsOneWidget,
    );
    expect(tester.getRect(expand), initialActionRect);
    expect(tester.getRect(cluster), initialClusterRect);
    expect(find.byKey(const Key('paw-camera')).hitTestable(), findsOneWidget);
    expect(find.byTooltip('語音輸入').hitTestable(), findsOneWidget);
    expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
    expect(input.hitTestable(), findsOneWidget);

    for (var cycle = 0; cycle < 2; cycle++) {
      await tester.tap(expand);
      await tester.pumpAndSettle();
      expect(
        find.byKey(const Key('work-surface')).hitTestable(),
        findsOneWidget,
      );
      expect(find.byTooltip('收合對話'), findsOneWidget);

      await tester.tap(expand);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);
      expect(find.byTooltip('展開對話'), findsOneWidget);
    }
    expect(tester.takeException(), isNull);
  });

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
        await tester.tap(find.byKey(const Key('paw-send')));
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
