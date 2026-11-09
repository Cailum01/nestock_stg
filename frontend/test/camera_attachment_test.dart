import 'dart:convert';
import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/presentation/agent_home.dart';
import 'package:nestock/features/photo/camera_capture.dart';
import 'package:nestock/features/photo/photo_upload.dart';
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

class FakeUpload implements PhotoUpload {
  @override
  Future<String> upload(Uint8List photo) async => 'opaque-test-ref';
}

class PendingUpload implements PhotoUpload {
  int calls = 0;
  Completer<String> pending = Completer<String>();
  @override
  Future<String> upload(Uint8List photo) {
    calls++;
    return pending.future;
  }
}

class TimeoutUpload implements PhotoUpload {
  int calls = 0;

  @override
  Future<String> upload(Uint8List photo) {
    calls++;
    return Future.error(TimeoutException('upload timed out'));
  }
}

void main() {
  testWidgets(
    'failed upload preserves draft and allows retry without duplicates',
    (tester) async {
      final uploader = PendingUpload();
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
          cameraCapture: FakeCamera(),
          photoUpload: uploader,
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '測試');
      await tester.tap(find.text('拍照'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('送出'));
      await tester.pump();
      expect(
        tester
            .widget<FilledButton>(find.widgetWithText(FilledButton, '送出'))
            .onPressed,
        isNull,
      );
      expect(uploader.calls, 1);
      uploader.pending.completeError(StateError('failed'));
      await tester.pumpAndSettle();
      expect(find.text('照片上傳失敗，請重試。'), findsOneWidget);
      expect(find.byKey(const Key('photo-attachment')), findsOneWidget);
      expect(tester.widget<TextField>(input).controller!.text, '測試');
      uploader.pending = Completer<String>();
      await tester.tap(find.text('送出'));
      await tester.pump();
      expect(uploader.calls, 2);
      const opaqueRef = 'opaque://Keep/Case?token=%2F+unchanged';
      uploader.pending.complete(opaqueRef);
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('message-photo')), findsOneWidget);
      final message = tester.widget<PhotoMessageImage>(
        find.byType(PhotoMessageImage),
      );
      expect(message.imageRef, opaqueRef);
      expect(message.bytes, await FakeCamera().capture());
    },
  );
  testWidgets('timeout upload preserves draft and photo for retry', (
    tester,
  ) async {
    final uploader = TimeoutUpload();
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(scenario: MockScenario.conversation),
        catalog: const DemoCatalog(),
        cameraCapture: FakeCamera(),
        photoUpload: uploader,
      ),
    );
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    await tester.enterText(input, '測試');
    await tester.tap(find.text('拍照'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();

    expect(find.text('照片上傳失敗，請重試。'), findsOneWidget);
    expect(find.byKey(const Key('photo-attachment')), findsOneWidget);
    expect(tester.widget<TextField>(input).controller!.text, '測試');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();
    expect(uploader.calls, 2);
  });
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
          photoUpload: FakeUpload(),
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, text);
      await tester.tap(find.text('拍照'));
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
          photoUpload: FakeUpload(),
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '備忘文字');
      await tester.tap(find.text('拍照'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('photo-attachment')), findsOneWidget);
      expect(tester.widget<TextField>(input).controller!.text, '備忘文字');
      await tester.tap(find.text('拍照'));
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
