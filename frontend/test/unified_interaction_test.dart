import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'agent_fixtures.dart';

class DelayedAgent implements AgentClient {
  final result = Completer<AgentReply>();
  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) =>
      result.future;
  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) => result.future;
}

class MixedAgent implements AgentClient {
  int calls = 0;
  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async {
    if (calls++ == 0) {
      return const AgentReply(
        message: '已辨識',
        mockVisionItems: [(name: '衛生紙', quantity: '3 包', location: '儲藏櫃')],
      );
    }
    return AgentReply(message: '回覆 $message');
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => const AgentReply(message: '完成');
}

void main() {
  testWidgets('normal messages stay textual without opening Vision fixtures', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(scenario: MockScenario.conversation),
        catalog: const DemoCatalog(),
      ),
    );
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    for (final message in ['家裡還有牛奶嗎？', '再說一次']) {
      await tester.enterText(input, message);
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      expect(find.text('辨識到 3 件物品'), findsNothing);
      expect(find.byKey(const Key('vision-item-0')), findsNothing);
    }
  });
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('unified conversation and vision scroll inside room at $size', (
      tester,
    ) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          agentClient: FixtureAgentClient(visionFixtureReply()),
          catalog: const DemoCatalog(),
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '查看照片');
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      final surface = find.byKey(const Key('work-surface'));
      final status = find.byKey(const Key('speech'));
      expect(
        find.descendant(of: status, matching: find.text('歡迎回家')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: status, matching: find.text('查看照片')),
        findsNothing,
      );
      expect(
        find.descendant(of: surface, matching: find.text('查看照片')),
        findsOneWidget,
      );
      expect(
        find.descendant(of: surface, matching: find.text('照片辨識示範')),
        findsOneWidget,
      );
      expect(find.text('照片辨識示範'), findsOneWidget);
      expect(
        find.descendant(
          of: surface,
          matching: find.byWidgetPredicate(
            (widget) =>
                widget is Scrollable &&
                widget.axisDirection == AxisDirection.down,
          ),
        ),
        findsOneWidget,
      );
      expect(
        tester.getTopLeft(find.text('查看照片')).dy,
        lessThan(tester.getTopLeft(find.text('照片辨識示範')).dy),
      );
      expect(
        tester.getTopLeft(find.text('照片辨識示範')).dy,
        lessThan(tester.getTopLeft(find.byKey(const Key('vision-item-0'))).dy),
      );
      final third = find.byKey(const Key('vision-item-2'));
      await tester.ensureVisible(third);
      await tester.pumpAndSettle();
      expect(third.hitTestable(), findsOneWidget);
      final confirm = find.text('確認全部入庫');
      await tester.ensureVisible(confirm);
      await tester.pumpAndSettle();
      expect(confirm.hitTestable(), findsOneWidget);
      final room = tester.getRect(find.byKey(const Key('room')));
      final panel = tester.getRect(surface);
      expect(panel.top, greaterThan(room.top));
      expect(panel.bottom, lessThanOrEqualTo(room.bottom));
      expect(input.hitTestable(), findsOneWidget);
      expect(find.byTooltip('語音輸入').hitTestable(), findsOneWidget);
      expect(find.byKey(const Key('toolbar')).hitTestable(), findsOneWidget);
      await tester.tap(find.byTooltip('關閉'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('顯示互動'));
      await tester.pumpAndSettle();
      expect(surface.hitTestable(), findsOneWidget);
      expect(find.text('辨識到 3 件物品'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }

  testWidgets('clear ignores a late response', (tester) async {
    final client = DelayedAgent();
    await tester.pumpWidget(
      NestockApp(agentClient: client, catalog: const DemoCatalog()),
    );
    await tester.enterText(find.byType(TextField), '等待中');
    await tester.tap(find.text('送出'));
    await tester.pump();
    expect(find.text('思考中...'), findsOneWidget);
    await tester.tap(find.text('清除對話'));
    await tester.pumpAndSettle();
    client.result.complete(const AgentReply(message: '過期回覆'));
    await tester.pumpAndSettle();
    expect(find.text('等待中'), findsNothing);
    expect(find.text('過期回覆'), findsNothing);
    expect(find.byKey(const Key('work-surface')), findsNothing);
    expect(find.text('歡迎回家'), findsOneWidget);
  });

  testWidgets('mixed turns remain ordered and preserve vision edits', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      NestockApp(agentClient: MixedAgent(), catalog: const DemoCatalog()),
    );
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    await tester.enterText(input, '照片');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();
    final quantity = find.byKey(const Key('quantity-0'));
    await tester.ensureVisible(quantity);
    await tester.enterText(quantity, '5');
    for (final message in ['後續一', '後續二', '後續三']) {
      await tester.enterText(input, message);
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
    }
    final scroll = tester.state<ScrollableState>(
      find.descendant(
        of: find.byKey(const Key('work-items')),
        matching: find.byWidgetPredicate(
          (widget) =>
              widget is Scrollable &&
              widget.axisDirection == AxisDirection.down,
        ),
      ),
    );
    expect(scroll.position.maxScrollExtent, greaterThan(0));
    expect(find.text('回覆 後續三').hitTestable(), findsOneWidget);
    expect(
      tester.getTopLeft(find.text('照片')).dy,
      lessThan(tester.getTopLeft(find.text('已辨識')).dy),
    );
    expect(
      tester.getTopLeft(find.text('已辨識')).dy,
      lessThan(tester.getTopLeft(find.byKey(const Key('vision-item-0'))).dy),
    );
    expect(
      tester.getTopLeft(find.byKey(const Key('vision-item-0'))).dy,
      lessThan(tester.getTopLeft(find.text('後續一')).dy),
    );
    expect(
      tester.getTopLeft(find.text('後續一')).dy,
      lessThan(tester.getTopLeft(find.text('回覆 後續一')).dy),
    );
    await tester.ensureVisible(quantity);
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(quantity).controller!.text, '5');
    expect(tester.takeException(), isNull);
  });
}
