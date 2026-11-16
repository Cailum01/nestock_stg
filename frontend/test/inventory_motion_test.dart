import 'fake_camera.dart';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/inventory/inventory_overlay.dart';
import 'package:nestock/features/inventory/inventory_view_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class Items implements InventoryViewClient {
  Items(this.count);
  final int count;
  @override
  Future<List<InventoryViewItem>> load() async =>
      List.generate(count, (i) => InventoryViewItem('物品$i', '1', '包'));
}

void main() {
  late FakeCamera testCamera;
  setUp(() => testCamera = FakeCamera());
  testWidgets('short panel wraps and long panel scrolls at its cap', (
    tester,
  ) async {
    Widget panel(int count) => MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topLeft,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 500, maxWidth: 350),
            child: InventoryOverlay(
              key: ValueKey(count),
              client: Items(count),
              onClose: () {},
            ),
          ),
        ),
      ),
    );
    await tester.pumpWidget(panel(1));
    await tester.pumpAndSettle();
    final short = tester
        .getSize(find.byKey(const Key('inventory-overlay')))
        .height;
    await tester.pumpWidget(panel(30));
    await tester.pumpAndSettle();
    final long = tester
        .getSize(find.byKey(const Key('inventory-overlay')))
        .height;
    expect(short, lessThan(long));
    expect(long, lessThanOrEqualTo(500));
    await tester.drag(find.byType(ListView), const Offset(0, -600));
    await tester.pumpAndSettle();
    expect(find.text('物品0'), findsNothing);
  });
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('inventory dismissal and clipping at $size', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          cameraCapture: testCamera,
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pump(const Duration(milliseconds: 60));
      final clip = tester.getRect(find.byKey(const Key('inventory-room-clip')));
      final room = tester.getRect(find.byKey(const Key('room')));
      expect(clip, room);
      await tester.pumpAndSettle();
      await tester.tapAt(Offset(room.right - 2, room.bottom - 2));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsOneWidget);
      await tester.tap(find.byTooltip('關閉庫存'));
      await tester.pump(const Duration(milliseconds: 60));
      expect(
        tester.getRect(find.byKey(const Key('inventory-room-clip'))),
        room,
      );
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      await tester.tap(
        find.descendant(
          of: find.byKey(const Key('composer')),
          matching: find.byType(TextField),
        ),
      );
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      await tester.enterText(
        find.descendant(
          of: find.byKey(const Key('composer')),
          matching: find.byType(TextField),
        ),
        '測試',
      );
      expect(find.text('測試'), findsOneWidget);
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('paw-camera')));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      expect(testCamera.calls, 1);
      expect(tester.takeException(), isNull);
    });
  }
}
