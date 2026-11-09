import 'dart:convert';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/speech/speech_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class TestRecorder implements SpeechRecorder {
  int startCalls = 0;
  int stopCalls = 0;

  @override
  Future<void> start() async {
    startCalls++;
  }

  @override
  Future<Uint8List> stop() async {
    stopCalls++;
    return Uint8List.fromList([1, 2, 3]);
  }

  @override
  Future<void> dispose() async {}
}

class TestSpeech implements SpeechClient {
  TestSpeech(this.value);
  final String value;
  @override
  Future<String> transcribe(Uint8List bytes) async => value;
}

class FailingSpeech implements SpeechClient {
  @override
  Future<String> transcribe(Uint8List bytes) async =>
      throw const SpeechHttpException(413);
}

void main() {
  testWidgets(
    'manual microphone Tooltip preserves press-hold-release recording',
    (tester) async {
      final recorder = TestRecorder();
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
          speechRecorder: recorder,
          speechClient: TestSpeech('語音內容'),
        ),
      );
      final microphone = find.byTooltip('語音輸入');
      final tooltip = tester.widget<Tooltip>(find.byType(Tooltip));
      expect(tooltip.triggerMode, TooltipTriggerMode.manual);

      final gesture = await tester.startGesture(tester.getCenter(microphone));
      await tester.pump();
      expect(recorder.startCalls, 1);
      await tester.pump(const Duration(milliseconds: 600));
      expect(recorder.stopCalls, 0);

      await gesture.up();
      await tester.pumpAndSettle();
      expect(recorder.stopCalls, 1);
      expect(
        tester
            .widget<TextField>(
              find.descendant(
                of: find.byKey(const Key('composer')),
                matching: find.byType(TextField),
              ),
            )
            .controller!
            .text,
        '語音內容',
      );
    },
  );

  testWidgets('backend error preserves draft and re-enables Send', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(scenario: MockScenario.conversation),
        catalog: const DemoCatalog(),
        speechRecorder: TestRecorder(),
        speechClient: FailingSpeech(),
      ),
    );
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    await tester.enterText(input, '保留');
    final gesture = await tester.startGesture(
      tester.getCenter(find.byTooltip('語音輸入')),
    );
    await tester.pump();
    await gesture.up();
    await tester.pumpAndSettle();
    expect(tester.widget<TextField>(input).controller!.text, '保留');
    expect(find.text('錄音檔過大，請重試。'), findsOneWidget);
    expect(
      tester
          .widget<FilledButton>(find.widgetWithText(FilledButton, '送出'))
          .onPressed,
      isNotNull,
    );
  });
  test('multipart transcription response', () async {
    final client = HttpSpeechClient(
      Uri.parse('http://localhost:8000'),
      client: MockClient((request) async {
        expect(request.url.path, '/api/speech/transcribe');
        expect(
          request.headers['content-type'],
          contains('multipart/form-data'),
        );
        expect(request.body, contains('recording.m4a'));
        return http.Response(
          jsonEncode({
            'transcript': '測試',
            'detected_language': 'zh-TW',
            'duration_seconds': 1.0,
          }),
          200,
          headers: {'content-type': 'application/json; charset=utf-8'},
        );
      }),
    );
    expect(await client.transcribe(Uint8List.fromList([1])), '測試');
  });
  for (final transcript in ['新增語音', '']) {
    testWidgets('speech preserves draft for transcript $transcript', (
      tester,
    ) async {
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(scenario: MockScenario.conversation),
          catalog: const DemoCatalog(),
          speechRecorder: TestRecorder(),
          speechClient: TestSpeech(transcript),
        ),
      );
      final input = find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      );
      await tester.enterText(input, '原本');
      final gesture = await tester.startGesture(
        tester.getCenter(find.byTooltip('語音輸入')),
      );
      await tester.pump();
      await gesture.up();
      await tester.pumpAndSettle();
      expect(
        tester.widget<TextField>(input).controller!.text,
        transcript.isEmpty ? '原本' : '原本 新增語音',
      );
      expect(find.byKey(const Key('work-surface')), findsNothing);
    });
  }
}
