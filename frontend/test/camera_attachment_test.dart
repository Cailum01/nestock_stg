import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/photo/camera_capture.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class FakeCamera implements CameraCapture {
  int calls = 0;
  @override
  Future<Uint8List?> capture() async {
    calls++;
    return base64Decode(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
    );
  }
}

void main() {
  for (final text in ['', '照片備註']) {
    testWidgets('send photo with text "$text"', (tester) async {
      tester.view.physicalSize = const Size(320, 568);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
          cameraCapture: FakeCamera(),
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, text);
      await tester.tap(find.text('拍照／照片'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('送出'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('photo-attachment')), findsNothing);
      expect(
        find.descendant(
          of: find.byKey(const Key('work-surface')),
          matching: find.byKey(const Key('message-photo')),
        ),
        findsOneWidget,
      );
      if (text.isNotEmpty) expect(find.text(text), findsOneWidget);
      expect(tester.widget<TextField>(input).controller!.text, isEmpty);
      expect(find.text('辨識到 3 件物品'), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
  testWidgets(
    'camera attachment preserves text and can be replaced or removed',
    (tester) async {
      tester.view.physicalSize = const Size(320, 568);
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      final camera = FakeCamera();
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
          cameraCapture: camera,
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '備忘文字');
      await tester.tap(find.text('拍照／照片'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('photo-attachment')), findsOneWidget);
      expect(tester.widget<TextField>(input).controller!.text, '備忘文字');
      await tester.tap(find.text('拍照／照片'));
      await tester.pumpAndSettle();
      expect(camera.calls, 2);
      expect(find.byKey(const Key('photo-attachment')), findsOneWidget);
      await tester.tap(find.byTooltip('移除照片'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('photo-attachment')), findsNothing);
      expect(tester.widget<TextField>(input).controller!.text, '備忘文字');
      expect(tester.takeException(), isNull);
    },
  );
}
