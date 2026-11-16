import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'agent_fixtures.dart';

void main() {
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets(
      'work surface is clipped at room boundary through motion at $size',
      (tester) async {
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
        await tester.enterText(find.byType(TextField), '查看照片');
        await tester.tap(find.byKey(const Key('paw-send')));
        await tester.pump();
        await tester.pump(const Duration(milliseconds: 50));
        final clip = find.byKey(const Key('room-work-clip'));
        expect(clip, findsOneWidget);
        expect(
          find.descendant(
            of: clip,
            matching: find.byKey(const Key('work-surface')),
          ),
          findsOneWidget,
        );
        final clipRect = tester.getRect(clip);
        final roomRect = tester.getRect(find.byKey(const Key('room')));
        expect(clipRect, roomRect);
        expect(
          clipRect.bottom,
          lessThanOrEqualTo(
            tester.getTopLeft(find.byKey(const Key('composer'))).dy,
          ),
        );
        expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
        await tester.pumpAndSettle();
        await tester.tap(find.byTooltip('關閉'));
        await tester.pump(const Duration(milliseconds: 60));
        expect(tester.getRect(clip), roomRect);
        expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
        await tester.pumpAndSettle();
        expect(
          find.byKey(const Key('work-surface')).hitTestable(),
          findsNothing,
        );
        expect(tester.takeException(), isNull);
      },
    );
  }
}
