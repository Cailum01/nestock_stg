import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/agent_result_card.dart';

void main() {
  testWidgets('unknown confirmation and missing quantity remain unconfirmed', (
    tester,
  ) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(
          body: SingleChildScrollView(
            child: AgentResultCard(
              result: AgentResultView(
                kind: ResultKind.vision,
                summary: '結果',
                data: {
                  'detected_items': [
                    {'canonical_name': '甲', 'quantity': 2, 'unit': '瓶'},
                    {
                      'canonical_name': '乙',
                      'quantity': 3,
                      'unit': '盒',
                      'unit_confirmed': 'true',
                    },
                    {
                      'canonical_name': '丙',
                      'unit': '包',
                      'unit_confirmed': false,
                    },
                    {'canonical_name': '丁', 'quantity': 'invalid', 'unit': '罐'},
                  ],
                },
              ),
            ),
          ),
        ),
      ),
    );
    expect(find.text('2 瓶（單位待確認）'), findsOneWidget);
    expect(find.text('3 盒（單位待確認）'), findsOneWidget);
    expect(find.text('單位：包（待確認）'), findsOneWidget);
    expect(find.text('單位：罐（待確認）'), findsOneWidget);
    expect(find.text('2 瓶'), findsNothing);
    expect(find.text('3 盒'), findsNothing);
    expect(find.textContaining('invalid'), findsNothing);
    expect(tester.takeException(), isNull);
  });
  testWidgets('single item and empty list keep the summary', (tester) async {
    Future<void> showResult(Map<String, dynamic> data) => tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: AgentResultCard(
            result: AgentResultView(
              kind: ResultKind.vision,
              summary: '辨識摘要',
              data: data,
            ),
          ),
        ),
      ),
    );
    await showResult({
      'detected_items': [
        {
          'canonical_name': '米',
          'quantity': 1,
          'unit': '包',
          'unit_confirmed': true,
        },
      ],
    });
    expect(find.text('米'), findsOneWidget);
    expect(find.text('1 包'), findsOneWidget);
    await showResult({'detected_items': []});
    expect(find.text('辨識摘要'), findsOneWidget);
    expect(find.text('米'), findsNothing);
    expect(find.text('僅辨識，尚未加入庫存'), findsOneWidget);
  });
  testWidgets(
    'renders multiple observations and uncertainty on a narrow screen',
    (tester) async {
      await tester.binding.setSurfaceSize(const Size(320, 568));
      addTearDown(() => tester.binding.setSurfaceSize(null));
      await tester.pumpWidget(
        MaterialApp(
          home: Scaffold(
            body: SingleChildScrollView(
              child: AgentResultCard(
                result: AgentResultView(
                  kind: ResultKind.vision,
                  summary: '照片辨識',
                  data: {
                    'detected_items': [
                      {
                        'canonical_name': '牛奶',
                        'quantity': 2.0,
                        'unit': '瓶',
                        'unit_confirmed': false,
                        'candidate_matches': [
                          {'name': '鮮奶'},
                          {'name': '豆奶'},
                        ],
                      },
                      {
                        'raw_detected_name': '咖啡',
                        'quantity': 2.5,
                        'unit': null,
                        'expiry_date': 12,
                        'candidate_matches': 'invalid',
                      },
                    ],
                  },
                ),
              ),
            ),
          ),
        ),
      );
      expect(find.text('僅辨識，尚未加入庫存'), findsOneWidget);
      expect(find.text('牛奶'), findsOneWidget);
      expect(find.text('咖啡'), findsOneWidget);
      expect(find.text('2 瓶（單位待確認）'), findsOneWidget);
      expect(find.text('2.5（單位待確認）'), findsOneWidget);
      expect(find.text('可能符合：鮮奶、豆奶'), findsOneWidget);
      expect(tester.takeException(), isNull);
    },
  );

  testWidgets('empty or malformed items retain summary', (tester) async {
    await tester.pumpWidget(
      const MaterialApp(
        home: Scaffold(
          body: AgentResultCard(
            result: AgentResultView(
              kind: ResultKind.vision,
              summary: '沒有結果',
              data: {
                'detected_items': [
                  null,
                  42,
                  {'quantity': []},
                ],
              },
            ),
          ),
        ),
      ),
    );
    expect(find.text('沒有結果'), findsOneWidget);
    expect(find.text('僅辨識，尚未加入庫存'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
