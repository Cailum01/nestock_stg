import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'camera_attachment_test.dart' as photo;

class VisionAgent implements PhotoAwareAgentClient {
  @override
  Future<AgentReply> send(String message) async =>
      sendWithPhoto(message, 'ref');
  @override
  Future<AgentReply> sendWithPhoto(String message, String imageRef) async =>
      const AgentReply(
        message: '照片辨識結果',
        results: [
          AgentResultView(
            kind: ResultKind.vision,
            summary: '觀察結果',
            data: {
              'detected_items': [
                {
                  'canonical_name': '牛奶',
                  'quantity': 2,
                  'unit': '瓶',
                  'unit_confirmed': true,
                },
                {
                  'canonical_name': '茶',
                  'quantity': 1,
                  'unit': '包',
                  'unit_confirmed': true,
                },
              ],
            },
          ),
        ],
      );
  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
  }) async => const AgentReply(message: '');
}

void main() {
  for (final size in [
    const Size(418, 975),
    const Size(390, 844),
    const Size(320, 568),
  ]) {
    testWidgets('photo and vision preserve usable scene at $size', (
      tester,
    ) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      tester.view.padding = const FakeViewPadding(top: 24, bottom: 24);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPadding);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpWidget(
        NestockApp(
          agentClient: VisionAgent(),
          catalog: const DemoCatalog(),
          cameraCapture: photo.FakeCamera(),
          photoUpload: photo.FakeUpload(),
        ),
      );
      final cart = find.byKey(const Key('cart'));
      final cabinet = find.byKey(const Key('cabinet'));
      expect(cart.hitTestable(), findsOneWidget);
      expect(cabinet.hitTestable(), findsOneWidget);
      final closedCart = tester.getRect(cart);
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '照片備註');
      await tester.tap(find.text('拍照／照片'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('message-photo')), findsOneWidget);
      expect(find.text('照片辨識結果'), findsOneWidget);
      expect(find.text('僅辨識，尚未加入庫存'), findsOneWidget);
      expect(cart, findsNothing);
      expect(cabinet, findsNothing);
      expect(
        tester.getRect(find.byKey(const Key('agent'))).bottom,
        lessThanOrEqualTo(
          tester.getRect(find.byKey(const Key('work-surface'))).top + .01,
        ),
      );
      expect(find.text('送出').hitTestable(), findsOneWidget);
      expect(find.text('購物清單').hitTestable(), findsOneWidget);
      expect(find.text('庫存').hitTestable(), findsOneWidget);
      for (var i = 0; i < 3; i++) {
        await tester.enterText(input, '後續訊息 $i');
        await tester.tap(find.text('送出'));
        await tester.pumpAndSettle();
      }
      final scroll = tester.state<ScrollableState>(
        find
            .descendant(
              of: find.byKey(const Key('work-items')),
              matching: find.byType(Scrollable),
            )
            .first,
      );
      expect(scroll.position.maxScrollExtent, greaterThan(0));
      expect(tester.takeException(), isNull);
      tester.view.viewInsets = const FakeViewPadding(bottom: 250);
      await tester.pumpAndSettle();
      expect(find.text('送出').hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
      tester.view.resetViewInsets();
      await tester.pumpAndSettle();
      await tester.tap(find.byTooltip('關閉'));
      await tester.pumpAndSettle();
      expect(tester.getRect(cart), closedCart);
      expect(cart.hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
}
