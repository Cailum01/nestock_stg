import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

import 'agent_fixtures.dart';

void main() {
  testWidgets('background collapses work surface without discarding edits', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: FixtureAgentClient(visionFixtureReply()),
        catalog: const DemoCatalog(),
      ),
    );
    await tester.enterText(find.byType(TextField), '查看照片');
    await tester.tap(find.byKey(const Key('paw-send')));
    await tester.pumpAndSettle();
    final quantity = find.byKey(const Key('quantity-0'));
    await tester.ensureVisible(quantity);
    await tester.enterText(quantity, '5');
    await tester.pumpAndSettle();
    final room = tester.getRect(find.byKey(const Key('room')));
    await tester.tapAt(Offset(room.center.dx, room.top + room.height * .05));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);
    await tester.tap(find.byKey(const Key('paw-expand-interaction')));
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(quantity).controller!.text, '5');
    expect(tester.takeException(), isNull);
  });
  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets(
      'room background dismisses focus and overlays while preserving draft at $size',
      (tester) async {
        tester.view.physicalSize = size;
        tester.view.devicePixelRatio = 1;
        addTearDown(tester.view.resetPhysicalSize);
        addTearDown(tester.view.resetDevicePixelRatio);
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
        await tester.tap(input);
        await tester.enterText(input, '保留草稿');
        await tester.pump();
        final editable = find.descendant(
          of: input,
          matching: find.byType(EditableText),
        );
        expect(
          tester.widget<EditableText>(editable).focusNode.hasFocus,
          isTrue,
        );
        final room = tester.getRect(find.byKey(const Key('room')));
        await tester.tapAt(
          Offset(room.center.dx, room.top + room.height * .05),
        );
        await tester.pumpAndSettle();
        expect(
          tester.widget<EditableText>(editable).focusNode.hasFocus,
          isFalse,
        );
        expect(tester.widget<TextField>(input).controller!.text, '保留草稿');
        await tester.tap(find.byKey(const Key('cabinet')));
        await tester.pumpAndSettle();
        expect(find.text('我的庫存'), findsOneWidget);
        await tester.tapAt(Offset(room.right - 5, room.bottom - 5));
        await tester.pumpAndSettle();
        expect(find.text('我的庫存'), findsNothing);
        expect(tester.widget<TextField>(input).controller!.text, '保留草稿');
        await tester.tap(find.byKey(const Key('cart')));
        await tester.pumpAndSettle();
        expect(find.text('購物清單'), findsWidgets);
        expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
        expect(tester.takeException(), isNull);
      },
    );
  }
}
