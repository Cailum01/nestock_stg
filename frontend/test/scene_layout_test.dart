import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/presentation/home_scene.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('scene, shortcuts and persistent composer at $size', (
      tester,
    ) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );
      expect(
        find.descendant(
          of: find.byType(HomeScene),
          matching: find.textContaining('歡迎回家'),
        ),
        findsOneWidget,
      );
      final room = tester.getRect(find.byKey(const Key('room')));
      final cabinet = tester.getRect(find.byKey(const Key('cabinet')));
      final cart = tester.getRect(find.byKey(const Key('cart')));
      final agent = tester.getRect(find.byKey(const Key('agent')));
      final speech = tester.getRect(find.byKey(const Key('speech')));
      final composer = tester.getRect(find.byKey(const Key('composer')));
      final toolbar = tester.getRect(find.byKey(const Key('toolbar')));
      expect(cabinet.center.dy, lessThan(room.center.dy));
      expect(cabinet.center.dx, lessThan(room.center.dx));
      expect(cart.center.dx, greaterThan(room.center.dx));
      expect(cart.top, greaterThan(room.top + room.height * .4));
      expect(
        room.contains(cart.topLeft) && room.contains(cart.bottomRight),
        isTrue,
      );
      expect(
        room.contains(agent.topLeft) && room.contains(agent.bottomRight),
        isTrue,
      );
      expect(cabinet.width, greaterThan(room.width * .2));
      expect(find.byKey(const Key('window')), findsOneWidget);
      expect(find.byKey(const Key('window-image')), findsOneWidget);
      expect(find.byKey(const Key('weather-viewport')), findsOneWidget);
      expect(
        find.descendant(
          of: find.byKey(const Key('weather-viewport')),
          matching: find.byWidgetPredicate(
            (widget) =>
                widget is Image &&
                widget.image is AssetImage &&
                (widget.image as AssetImage).assetName ==
                    'assets/images/weather/weather_test.png' &&
                widget.fit == BoxFit.cover,
          ),
        ),
        findsOneWidget,
      );
      final sceneWindow = tester.widget<Stack>(
        find
            .descendant(
              of: find.byType(SceneWindow),
              matching: find.byType(Stack),
            )
            .first,
      );
      expect(sceneWindow.children.first, isA<Positioned>());
      expect(
        find.descendant(
          of: find.byKey(const Key('weather-viewport')),
          matching: find.byKey(const Key('window-image')),
        ),
        findsNothing,
      );
      expect(
        (agent.center.dy - room.center.dy).abs(),
        lessThan(room.height * .3),
      );
      expect(speech.center.dy, lessThan(agent.center.dy));
      expect(speech.center.dx, greaterThan(agent.center.dx));
      expect(composer.top, greaterThanOrEqualTo(room.bottom));
      expect(toolbar.top, greaterThanOrEqualTo(composer.bottom));
      expect(find.byType(TextField).hitTestable(), findsOneWidget);
      for (final pair in [
        ('cabinet', '白米'),
        ('cart', '牛奶'),
        ('庫存', '白米'),
        ('購物清單', '牛奶'),
      ]) {
        final target = pair.$1 == 'cabinet' || pair.$1 == 'cart'
            ? find.byKey(Key(pair.$1))
            : find.text(pair.$1);
        await tester.ensureVisible(target);
        await tester.pumpAndSettle();
        await tester.tap(target);
        await tester.pumpAndSettle();
        expect(find.text(pair.$2), findsOneWidget);
        if (find.byTooltip('關閉庫存').evaluate().isNotEmpty) {
          await tester.tap(find.byTooltip('關閉庫存'));
        } else if (find.byTooltip('關閉購物清單').evaluate().isNotEmpty) {
          await tester.tap(find.byTooltip('關閉購物清單'));
        } else {
          await tester.pageBack();
        }
        await tester.pumpAndSettle();
      }
      expect(find.byType(TextField).hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    });
  }
}
