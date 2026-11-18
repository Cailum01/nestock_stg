import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/presentation/home_scene.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  testWidgets(
    'idle Noki breathes and wanders without affecting scene controls',
    (tester) async {
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );

      expect(find.byKey(const Key('agent')), findsOneWidget);
      expect(find.byKey(const Key('cabinet')).hitTestable(), findsOneWidget);
      expect(find.byKey(const Key('cart')).hitTestable(), findsOneWidget);
      expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
      expect(find.text('歡迎回家。'), findsOneWidget);

      await tester.pump(const Duration(seconds: 1));
      await tester.pump(const Duration(milliseconds: 700));

      final transform = tester.widget<Transform>(
        find.byKey(const Key('noki-idle-breath')),
      );
      expect(transform.transform.getMaxScaleOnAxis(), greaterThan(1));

      await tester.pump(const Duration(seconds: 6));
      await tester.pump(const Duration(milliseconds: 2500));
      expect(
        tester
            .widget<Transform>(find.byKey(const Key('noki-idle-wander')))
            .transform
            .storage[12],
        lessThan(0),
      );
      expect(
        tester
            .widget<Transform>(find.byKey(const Key('noki-idle-sprite')))
            .transform
            .storage[0],
        greaterThan(0),
      );

      await tester.pump(const Duration(milliseconds: 2501));
      await tester.pump(const Duration(milliseconds: 2));
      await tester.pump(const Duration(milliseconds: 2500));
      expect(
        tester
            .widget<Transform>(find.byKey(const Key('noki-idle-sprite')))
            .transform
            .storage[0],
        lessThan(0),
      );
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('idle bubble cycles separately from the mirrored sprite', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(agentClient: MockAgentClient(), catalog: const DemoCatalog()),
    );

    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);
    expect(find.text('歡迎回家。'), findsOneWidget);
    await tester.pump(const Duration(seconds: 3));
    expect(find.byKey(const Key('speech')), findsNothing);
    await tester.pump(const Duration(seconds: 10));
    await tester.pump(const Duration(milliseconds: 350));

    final bubble = find.byKey(const Key('idle-noki-bubble'));
    expect(bubble, findsOneWidget);
    expect(find.text('今天也要整理得整整齊齊。'), findsOneWidget);
    expect(
      find.descendant(
        of: find.byKey(const Key('noki-idle-sprite')),
        matching: bubble,
      ),
      findsNothing,
    );

    final bubbleCenter = tester.getCenter(find.byKey(const Key('speech')));
    await tester.pump(const Duration(seconds: 1));
    expect(
      tester.getCenter(find.byKey(const Key('speech')).first).dx,
      lessThan(bubbleCenter.dx),
    );

    await tester.pump(const Duration(seconds: 5));
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pump();
    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);
  });

  testWidgets('status bubble takes precedence over idle chatter', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 390,
            height: 640,
            child: HomeScene(
              statusMessage: '思考中...',
              onInventory: () {},
              onShopping: () {},
              onBackgroundTap: () {},
            ),
          ),
        ),
      ),
    );

    await tester.pump(const Duration(seconds: 8));
    expect(find.text('思考中...'), findsOneWidget);
    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);
  });

  testWidgets('thought bubble grows from its Noki-side anchor and wraps text', (
    tester,
  ) async {
    const shortMessage = '要不要看看庫存？';
    const singleLineMessage = '今天也要整理得整整齊齊。';
    const longMessage = '今天也要整理得整整齊齊，別忘了看看家裡還缺什麼。';

    Future<void> pumpScene(String message) => tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 390,
            height: 640,
            child: HomeScene(
              statusMessage: message,
              onInventory: () {},
              onShopping: () {},
              onBackgroundTap: () {},
            ),
          ),
        ),
      ),
    );

    await pumpScene(shortMessage);
    final shortBubble = tester.getSize(find.byKey(const Key('speech')));
    final shortText = tester.widget<Text>(find.text(shortMessage));
    expect(shortText.style?.letterSpacing, isNull);

    await pumpScene(singleLineMessage);
    final singleLineBubble = tester.getSize(find.byKey(const Key('speech')));
    final singleLineText = tester.getSize(find.text(singleLineMessage));
    final textArea = tester.getSize(
      find.byKey(const Key('noki-thought-text-area')),
    );
    expect(singleLineBubble.width, greaterThan(singleLineBubble.height));
    expect(singleLineBubble.height, closeTo(120, .1));
    expect(singleLineBubble.width, greaterThan(shortBubble.width));
    expect(singleLineBubble.height, closeTo(shortBubble.height, .1));
    expect(singleLineText.height, lessThanOrEqualTo(24));
    expect(
      singleLineBubble.width - singleLineText.width,
      greaterThanOrEqualTo(52 - .1),
    );
    expect(textArea.width, closeTo(singleLineBubble.width, .1));
    expect(
      tester
          .widget<Transform>(find.byKey(const Key('noki-thought-text-offset')))
          .transform
          .storage[13],
      closeTo(-7, .1),
    );

    await pumpScene(longMessage);
    final longBubble = tester.getSize(find.byKey(const Key('speech')));
    final longText = tester.getSize(find.text(longMessage));
    expect(longBubble.width, greaterThan(singleLineBubble.width));
    expect(longBubble.width, lessThanOrEqualTo(374));
    expect(longBubble.height, lessThanOrEqualTo(singleLineBubble.height + 20));
    expect(longText.height, greaterThan(20));
  });

  testWidgets('direction change keeps the sprite centered at the endpoint', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(agentClient: MockAgentClient(), catalog: const DemoCatalog()),
    );

    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(milliseconds: 4999));
    final beforeGroupOffset = tester
        .widget<Transform>(find.byKey(const Key('noki-idle-wander')))
        .transform
        .storage[12];
    final beforeCenter = tester.getCenter(
      find.byKey(const Key('noki-idle-sprite-content')),
    );
    expect(
      tester
          .widget<Transform>(find.byKey(const Key('noki-idle-sprite')))
          .transform
          .storage[0],
      greaterThan(0),
    );

    await tester.pump(const Duration(milliseconds: 3));
    final afterGroupOffset = tester
        .widget<Transform>(find.byKey(const Key('noki-idle-wander')))
        .transform
        .storage[12];
    final afterCenter = tester.getCenter(
      find.byKey(const Key('noki-idle-sprite-content')),
    );
    expect(afterGroupOffset, closeTo(-48, .01));
    expect(afterGroupOffset, closeTo(beforeGroupOffset, .1));
    expect(afterCenter.dx, closeTo(beforeCenter.dx, .1));
    expect(
      tester
          .widget<Transform>(find.byKey(const Key('noki-idle-sprite')))
          .transform
          .storage[0],
      lessThan(0),
    );
  });

  testWidgets('wide thought bubble stays on-screen at both wander extremes', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            width: 390,
            height: 640,
            child: HomeScene(
              statusMessage: '今天也要整理得整整齊齊，別忘了看看家裡還缺什麼。',
              onInventory: () {},
              onShopping: () {},
              onBackgroundTap: () {},
            ),
          ),
        ),
      ),
    );

    void expectBubbleOnScreen() {
      final bubble = tester.getRect(find.byKey(const Key('speech')));
      expect(bubble.left, greaterThanOrEqualTo(0));
      expect(bubble.right, lessThanOrEqualTo(390));
      expect(bubble.width, lessThanOrEqualTo(374));
    }

    expectBubbleOnScreen();
    await tester.pump(const Duration(seconds: 11));
    await tester.pump(const Duration(milliseconds: 3));
    expectBubbleOnScreen();

    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(seconds: 5));
    await tester.pump(const Duration(milliseconds: 3));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(seconds: 5));
    await tester.pump(const Duration(milliseconds: 3));
    expectBubbleOnScreen();
  });

  testWidgets(
    'long bubble uses viewport width and shifts left of right-side Noki',
    (tester) async {
      tester.view.physicalSize = const Size(390, 640);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);

      const longMessage = '購物車是不是又多了東西...';
      const shortMessage = '要不要看看庫存？';

      Future<void> pumpScene(String message) => tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: HomeScene(
              statusMessage: message,
              onInventory: () {},
              onShopping: () {},
              onBackgroundTap: () {},
            ),
          ),
        ),
      );

      await pumpScene(shortMessage);
      final shortBubble = tester.getRect(find.byKey(const Key('speech')));

      await pumpScene(longMessage);
      final bubbleFinder = find.byKey(const Key('speech'));
      final textFinder = find.text(longMessage);

      void expectSafeOneLineBubble() {
        final bubble = tester.getRect(bubbleFinder);
        final text = tester.getRect(textFinder);
        final visibleLeftEdge = bubble.left + bubble.width * .13;
        final visibleRightEdge = bubble.right - bubble.width * .145;

        expect(bubble.left, greaterThanOrEqualTo(8));
        expect(bubble.right, lessThanOrEqualTo(382));
        expect(text.height, lessThanOrEqualTo(20.1));
        expect(text.left - visibleLeftEdge, greaterThanOrEqualTo(24));
        expect(visibleRightEdge - text.right, greaterThanOrEqualTo(24));
        expect(tester.takeException(), isNull);
      }

      final initialBubble = tester.getRect(bubbleFinder);
      expect(initialBubble.width, greaterThan(shortBubble.width));
      expectSafeOneLineBubble();

      // Reach the +48 px endpoint: 0 -> -48 -> 0 -> +48.
      await tester.pump(const Duration(seconds: 6));
      await tester.pump(const Duration(milliseconds: 4999));
      await tester.pump(const Duration(milliseconds: 3));
      expectSafeOneLineBubble();
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump(const Duration(milliseconds: 4999));
      await tester.pump(const Duration(milliseconds: 3));
      await tester.pump(const Duration(milliseconds: 200));
      await tester.pump(const Duration(milliseconds: 4999));

      final rightSideBubble = tester.getRect(bubbleFinder);
      final wanderOffset = tester
          .widget<Transform>(find.byKey(const Key('noki-idle-wander')))
          .transform
          .storage[12];
      final nominalNoki = tester.getRect(find.byKey(const Key('agent')));
      final preferredBubbleLeft =
          nominalNoki.left + wanderOffset + nominalNoki.width * .36 + 12;
      expectSafeOneLineBubble();
      expect(wanderOffset, closeTo(48, .1));
      expect(rightSideBubble.left, lessThan(preferredBubbleLeft));
      expect(
        rightSideBubble.center.dx,
        lessThan(nominalNoki.center.dx + wanderOffset),
      );
      expect(rightSideBubble.left, closeTo(initialBubble.left, .1));
    },
  );

  testWidgets('initial welcome completes before idle chatter is scheduled', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(agentClient: MockAgentClient(), catalog: const DemoCatalog()),
    );

    expect(find.text('歡迎回家。'), findsOneWidget);
    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);

    await tester.pump(const Duration(seconds: 2));
    expect(find.text('歡迎回家。'), findsOneWidget);
    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);

    await tester.pump(const Duration(seconds: 1));
    expect(find.byKey(const Key('speech')), findsNothing);
    await tester.pump(const Duration(seconds: 9));
    expect(find.byKey(const Key('idle-noki-bubble')), findsNothing);

    await tester.pump(const Duration(seconds: 1));
    await tester.pump(const Duration(milliseconds: 350));
    expect(find.byKey(const Key('idle-noki-bubble')), findsOneWidget);
  });
}
