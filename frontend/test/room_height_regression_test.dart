import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

void main() {
  testWidgets(
    'overlay constraints stay normalized when keyboard shrinks room below 32',
    (tester) async {
      tester.view.physicalSize = const Size(320, 568);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      addTearDown(tester.view.resetViewInsets);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
        ),
      );
      final roomHeight = tester.getSize(find.byKey(const Key('room'))).height;
      tester.view.viewInsets = FakeViewPadding(bottom: roomHeight - 20);
      await tester.pumpAndSettle();
      expect(
        tester.getSize(find.byKey(const Key('room'))).height,
        lessThan(32),
      );
      expect(tester.takeException(), isNull);
    },
  );
}
