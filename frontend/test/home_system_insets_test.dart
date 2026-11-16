import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show SystemUiOverlayStyle;
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  testWidgets('wide home viewport paints the room base behind artwork', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(540, 900);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(
      NestockApp(agentClient: MockAgentClient(), catalog: const DemoCatalog()),
    );

    final viewport = find.byKey(const Key('home-viewport'));
    final roomBackgroundBase = find.byKey(const Key('room-background-base'));
    expect(roomBackgroundBase, findsOneWidget);
    expect(
      tester.widget<ColoredBox>(roomBackgroundBase).color,
      const Color(0xFFEAEDF5),
    );
    expect(tester.getRect(roomBackgroundBase), tester.getRect(viewport));
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'home viewport respects bars and keyboard without double bottom padding',
    (tester) async {
      tester.view.physicalSize = const Size(418, 975);
      tester.view.devicePixelRatio = 1;
      tester.view.padding = const FakeViewPadding(top: 32, bottom: 28);
      tester.view.viewPadding = const FakeViewPadding(top: 32, bottom: 28);
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetPadding);
      addTearDown(tester.view.resetViewPadding);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );
      final layer = tester.binding.renderViews.first.debugLayer!;
      final style = layer.find<SystemUiOverlayStyle>(const Offset(209, 16));
      final navigationStyle = layer.find<SystemUiOverlayStyle>(
        const Offset(209, 961),
      );
      expect(style?.statusBarIconBrightness, Brightness.light);
      expect(
        navigationStyle?.systemNavigationBarIconBrightness,
        Brightness.light,
      );
      expect(style?.statusBarColor, Colors.black);
      expect(navigationStyle?.systemNavigationBarColor, Colors.black);
      final viewport = find.byKey(const Key('home-viewport'));
      final roomBackgroundBase = find.byKey(const Key('room-background-base'));
      expect(roomBackgroundBase, findsOneWidget);
      expect(
        tester.widget<ColoredBox>(roomBackgroundBase).color,
        const Color(0xFFEAEDF5),
      );
      expect(tester.getRect(roomBackgroundBase), tester.getRect(viewport));
      expect(tester.getRect(viewport).top, 32);
      expect(tester.getRect(viewport).bottom, 975 - 28);
      expect(
        tester.getRect(find.text('Nestock｜窩有')).top,
        greaterThanOrEqualTo(32),
      );
      expect(
        tester.getRect(find.byKey(const Key('paw-action-cluster'))).bottom,
        lessThanOrEqualTo(975 - 28),
      );
      tester.view.viewInsets = const FakeViewPadding(bottom: 280);
      tester.view.padding = const FakeViewPadding(top: 32);
      await tester.pumpAndSettle();
      expect(tester.getRect(viewport).bottom, 975);
      expect(
        tester.getRect(find.byKey(const Key('paw-action-cluster'))).bottom,
        lessThanOrEqualTo(975 - 280),
      );
      expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );
}
