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
      final room = tester.getRect(find.byKey(const Key('room')));
      final cabinet = tester.getRect(find.byKey(const Key('cabinet')));
      final cart = tester.getRect(find.byKey(const Key('cart')));
      final agent = tester.getRect(find.byKey(const Key('agent')));
      final composer = tester.getRect(find.byKey(const Key('composer')));
      final actionCluster = tester.getRect(
        find.byKey(const Key('paw-action-cluster')),
      );
      final composerFade = find.byKey(const Key('composer-background-fade'));
      final layoutColumn = tester.widget<Column>(
        find.byKey(const Key('home-layout-column')),
      );
      final sceneLayer = layoutColumn.children.indexWhere(
        (child) => child.key == const Key('scene-layer'),
      );
      final composerLayer = layoutColumn.children.indexWhere(
        (child) => child.key == const Key('composer'),
      );
      final composerStack = tester.widget<Stack>(
        find
            .descendant(
              of: find.byKey(const Key('composer')),
              matching: find.byType(Stack),
            )
            .first,
      );
      final composerFadeLayer = composerStack.children.indexWhere(
        (child) => child.key == const Key('composer-background-fade-layer'),
      );
      final sceneStack = tester.widget<Stack>(
        find.byKey(const Key('room-scene-stack')),
      );
      final cabinetLayer = sceneStack.children.indexWhere(
        (child) => child.key == const Key('cabinet-layer'),
      );
      final beanbagLayer = sceneStack.children.indexWhere(
        (child) => child.key == const Key('beanbag-layer'),
      );
      expect(cabinet.center.dy, lessThan(room.center.dy));
      expect(cabinet.center.dx, lessThan(room.center.dx));
      expect(cart.center.dx, greaterThan(room.center.dx));
      expect(cart.top, greaterThan(room.top + room.height * .4));
      // The approved shopping-cart placement is anchored near the room's right
      // edge while remaining visibly within its vertical bounds.
      expect(cart.top, greaterThanOrEqualTo(room.top));
      expect(cart.bottom, lessThanOrEqualTo(room.bottom));
      expect(cart.left, lessThan(room.right));
      expect(cart.right, lessThanOrEqualTo(room.right));
      expect(cart.right, greaterThan(room.right - room.width * .05));
      // Noki's approved lower-room illustration may extend beyond the scene
      // bounds, but it must remain visibly anchored in the lower half.
      expect(room.overlaps(agent), isTrue);
      expect(agent.center.dy, greaterThan(room.center.dy));
      expect(cabinet.width, greaterThan(room.width * .2));
      expect(find.byKey(const Key('window')), findsOneWidget);
      expect(find.byKey(const Key('window-image')), findsOneWidget);
      expect(find.byKey(const Key('weather-viewport')), findsOneWidget);
      final ambience = tester.widget<Image>(
        find.byKey(const Key('window-ambience-image')),
      );
      expect(ambience.image, isA<AssetImage>());
      expect(
        (ambience.image as AssetImage).assetName,
        isIn(windowAmbienceAssets.values.expand((assets) => assets)),
      );
      expect(ambience.fit, BoxFit.cover);
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
        // Noki is intentionally composed lower in the approved scene.
        lessThan(room.height * .4),
      );
      expect(find.byKey(const Key('noki-idle-wander')), findsOneWidget);
      expect(find.text('歡迎回家。'), findsOneWidget);
      expect(composer.top, greaterThanOrEqualTo(room.bottom));
      expect(actionCluster.bottom, lessThanOrEqualTo(composer.bottom));
      expect(composerFade, findsOneWidget);
      final fadeDecoration =
          tester.widget<DecoratedBox>(composerFade).decoration as BoxDecoration;
      final fade = fadeDecoration.gradient! as LinearGradient;
      expect(fade.begin, Alignment.topCenter);
      expect(fade.end, Alignment.bottomCenter);
      expect(fade.colors.first.a, 0);
      expect(fade.colors.last, const Color(0xFFEAEDF5));
      // The scene paints first. The composer gradient then overlaps the lower
      // scene, with opaque controls painted above that gradient.
      expect(tester.getRect(composerFade).top, lessThan(room.bottom));
      final fadeRect = tester.getRect(composerFade);
      expect(agent.bottom, greaterThan(fadeRect.top));
      expect(agent.top, lessThan(fadeRect.bottom));
      expect(sceneLayer, greaterThanOrEqualTo(0));
      expect(composerLayer, greaterThan(sceneLayer));
      expect(composerFadeLayer, 0);
      expect(cabinetLayer, greaterThanOrEqualTo(0));
      expect(beanbagLayer, greaterThan(cabinetLayer));
      for (final action in [
        'paw-camera',
        'paw-voice',
        'paw-expand-interaction',
      ]) {
        final actionFinder = find.byKey(Key(action));
        expect(actionFinder.hitTestable(), findsOneWidget);
        expect(tester.getSize(actionFinder), const Size(48, 48));
      }
      expect(
        tester.getSize(find.byKey(const Key('paw-send'))),
        const Size(64, 64),
      );
      expect(find.byType(TextField).hitTestable(), findsOneWidget);
      for (final pair in [('cabinet', '白米'), ('cart', '牛奶')]) {
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
