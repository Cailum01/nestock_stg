import 'fake_camera.dart';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'agent_fixtures.dart';

void main() {
  late FakeCamera testCamera;
  setUp(() => testCamera = FakeCamera());
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('shopping dismissal and room clipping at $size', (
      tester,
    ) async {
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
      Future<void> open() async {
        await tester.tap(find.byKey(const Key('cart')));
        await tester.pumpAndSettle();
      }

      await tester.tap(find.byKey(const Key('cart')));
      await tester.pump(const Duration(milliseconds: 60));
      final clip = find.byKey(const Key('shopping-room-clip'));
      expect(
        tester.getRect(clip),
        tester.getRect(find.byKey(const Key('room'))),
      );
      expect(
        tester.getRect(clip).bottom,
        lessThanOrEqualTo(
          tester.getTopLeft(find.byKey(const Key('composer'))).dy,
        ),
      );
      await tester.pumpAndSettle();
      expect(
        tester.getCenter(find.byTooltip('關閉購物清單')).dx,
        greaterThan(tester.getCenter(find.byKey(const Key('cart'))).dx),
      );
      final room = tester.getRect(find.byKey(const Key('room')));
      await tester.tapAt(Offset(room.left + 2, room.bottom - 2));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      await open();
      expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pump(const Duration(milliseconds: 60));
      expect(tester.getRect(clip), room);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      await open();
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      await open();
      final composer = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.tap(composer);
      await tester.pumpAndSettle();
      await tester.enterText(composer, '測試');
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(find.text('測試'), findsOneWidget);
      await open();
      await tester.tap(find.byKey(const Key('paw-camera')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(testCamera.calls, 1);
      await tester.pump(const Duration(seconds: 5));
      await tester.pumpAndSettle();
      await open();
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(find.text('我的庫存'), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets('shopping preserves edited work surface', (tester) async {
    await tester.pumpWidget(
      NestockApp(
        cameraCapture: testCamera,
        agentClient: FixtureAgentClient(visionFixtureReply()),
        catalog: const DemoCatalog(),
      ),
    );
    await tester.enterText(find.byType(TextField), '查看照片');
    await tester.tap(find.byKey(const Key('paw-send')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('quantity-0')), '5');
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('關閉'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('cart')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);
    await tester.tap(find.byTooltip('關閉購物清單'));
    await tester.pumpAndSettle();
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('quantity-0')))
          .controller!
          .text,
      '5',
    );
    await tester.tap(find.byKey(const Key('paw-expand-interaction')));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsOneWidget);
  });
}
