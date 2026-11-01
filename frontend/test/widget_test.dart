import 'fake_camera.dart';

import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/agent_result_card.dart';
import 'package:nestock/shared/data/catalog_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';
import 'package:nestock/shared/widgets/async_state.dart';
import 'package:nestock/shared/widgets/item_list.dart';

class ControlledAgent implements AgentClient {
  Completer<AgentReply> pending = Completer<AgentReply>();
  final List<String> messages = [];
  final List<(String, bool)> decisions = [];
  @override
  Future<AgentReply> send(String message) {
    messages.add(message);
    return pending.future;
  }

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  }) {
    decisions.add((approvalId, approved));
    return pending.future;
  }
}

Future<void> sendMessage(WidgetTester tester) async {
  await tester.enterText(find.byType(TextField), '查看庫存');
  await tester.tap(find.text('送出'));
  await tester.pump();
}

void main() {
  late FakeCamera testCamera;
  setUp(() => testCamera = FakeCamera());
  testWidgets('scene opens independent inventory and shopping routes', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        cameraCapture: testCamera,
        agentClient: MockAgentClient(),
        catalog: const DemoCatalog(),
      ),
    );
    expect(find.text('Nestock｜窩有'), findsOneWidget);
    expect(find.byType(NavigationBar), findsNothing);
    expect(find.byType(BottomNavigationBar), findsNothing);
    await tester.tap(find.byKey(const Key('cabinet')));
    await tester.pumpAndSettle();
    expect(find.text('白米'), findsOneWidget);
    if (find.byTooltip('關閉庫存').evaluate().isNotEmpty) {
      await tester.tap(find.byTooltip('關閉庫存'));
    } else if (find.byTooltip('關閉購物清單').evaluate().isNotEmpty) {
      await tester.tap(find.byTooltip('關閉購物清單'));
    } else {
      await tester.pageBack();
    }
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('cart')));
    await tester.pumpAndSettle();
    expect(find.text('牛奶'), findsOneWidget);
    await tester.tap(find.byTooltip('關閉購物清單'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('拍照／照片'));
    await tester.pumpAndSettle();
    expect(testCamera.calls, 1);
  });

  for (final approved in [true, false]) {
    testWidgets('mock decision $approved never changes the catalog', (
      tester,
    ) async {
      const catalog = DemoCatalog();
      final before = (await catalog.shopping())
          .map((item) => item.detail)
          .toList();
      await tester.pumpWidget(
        NestockApp(
          cameraCapture: testCamera,
          agentClient: MockAgentClient(),
          catalog: catalog,
        ),
      );
      await sendMessage(tester);
      await tester.pumpAndSettle();
      expect(find.byType(AgentResultCard), findsOneWidget);
      final button = find.text(approved ? '確認示範' : '取消');
      await tester.ensureVisible(button);
      await tester.pumpAndSettle();
      await tester.tap(button);
      await tester.pumpAndSettle();
      expect(
        find.text(approved ? '確認流程示範結束，沒有儲存任何變更。' : '已取消示範，沒有儲存任何變更。'),
        findsOneWidget,
      );
      expect(
        (await catalog.shopping()).map((item) => item.detail).toList(),
        before,
      );
    });
  }

  testWidgets(
    'injected client exposes loading, guards duplicate send and retries errors',
    (tester) async {
      final client = ControlledAgent();
      await tester.pumpWidget(
        NestockApp(
          cameraCapture: testCamera,
          agentClient: client,
          catalog: const DemoCatalog(),
        ),
      );
      await tester.tap(find.text('送出'));
      expect(client.messages, isEmpty);
      await sendMessage(tester);
      expect(find.byType(LoadingState), findsOneWidget);
      await tester.tap(find.text('送出'));
      expect(client.messages, ['查看庫存']);
      client.pending.completeError(Exception('offline'));
      await tester.pumpAndSettle();
      expect(find.byType(ErrorState), findsOneWidget);
      client.pending = Completer<AgentReply>();
      await tester.tap(find.text('重試'));
      await tester.pump();
      expect(client.messages, ['查看庫存', '查看庫存']);
      client.pending.complete(const AgentReply(message: '讀取成功'));
      await tester.pumpAndSettle();
      expect(find.text('讀取成功'), findsOneWidget);
    },
  );

  testWidgets('resume retry preserves approval identity and decision', (
    tester,
  ) async {
    final client = ControlledAgent();
    await tester.pumpWidget(
      NestockApp(
        cameraCapture: testCamera,
        agentClient: client,
        catalog: const DemoCatalog(),
      ),
    );
    await sendMessage(tester);
    client.pending.complete(
      const AgentReply(
        message: '確認',
        approval: ApprovalView(
          id: 'approval-stable',
          presentation: ApprovalPresentation.singleAction,
          operations: [
            OperationView(id: 'operation-stable', description: '牛奶 1 瓶'),
          ],
        ),
      ),
    );
    await tester.pumpAndSettle();
    client.pending = Completer<AgentReply>();
    await tester.ensureVisible(find.text('確認示範'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('確認示範'));
    await tester.pump();
    client.pending.completeError(Exception('lost response'));
    await tester.pumpAndSettle();
    client.pending = Completer<AgentReply>();
    await tester.ensureVisible(find.text('重試'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('重試'));
    await tester.pump();
    expect(client.decisions, [
      ('approval-stable', true),
      ('approval-stable', true),
    ]);
    client.pending.complete(const AgentReply(message: '完成示範'));
    await tester.pumpAndSettle();
    expect(find.text('完成示範'), findsOneWidget);
  });

  testWidgets('list supports loading, failure, retry and empty state', (
    tester,
  ) async {
    var pending = Completer<List<ItemSummary>>();
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: ItemList(load: () => pending.future, emptyMessage: '沒有品項'),
        ),
      ),
    );
    expect(find.byType(LoadingState), findsOneWidget);
    pending.completeError(Exception('offline'));
    await tester.pumpAndSettle();
    expect(find.byType(ErrorState), findsOneWidget);
    pending = Completer<List<ItemSummary>>();
    await tester.tap(find.text('重試'));
    await tester.pump();
    pending.complete([]);
    await tester.pumpAndSettle();
    expect(find.text('沒有品項'), findsOneWidget);
  });

  testWidgets('late response after scene disposal is ignored', (tester) async {
    final client = ControlledAgent();
    await tester.pumpWidget(
      NestockApp(
        cameraCapture: testCamera,
        agentClient: client,
        catalog: const DemoCatalog(),
      ),
    );
    await sendMessage(tester);
    await tester.pumpWidget(const SizedBox());
    client.pending.complete(const AgentReply(message: 'late'));
    await tester.pumpAndSettle();
    expect(tester.takeException(), isNull);
  });
}
