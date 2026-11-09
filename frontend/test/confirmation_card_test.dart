import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';
import 'package:nestock/features/agent/presentation/confirmation_card.dart';
import 'package:nestock/features/speech/speech_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';
import 'package:nestock/shared/theme/app_theme.dart';

import 'agent_fixtures.dart';

const _items = [
  ConfirmationItemView(name: '高纖無糖豆漿', quantity: 1, unit: '瓶', location: '冰箱'),
  ConfirmationItemView(name: '衛生紙', quantity: 2, unit: '包', location: '儲藏櫃'),
];

const _longItems = [
  ConfirmationItemView(
    name: '特濃無糖高蛋白豆漿家庭分享包',
    quantity: 12,
    unit: '大型家庭補充包',
    location: '冰箱最上層',
  ),
];

Widget _card({
  List<ConfirmationItemView> items = _items,
  TextScaler? textScaler,
  VoidCallback? onCancel,
}) => MaterialApp(
  builder: textScaler == null
      ? null
      : (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(textScaler: textScaler),
          child: child!,
        ),
  home: Scaffold(
    body: SingleChildScrollView(
      child: ConfirmationCard(
        items: items,
        onCancel: onCancel ?? _noop,
        onConfirm: _noop,
      ),
    ),
  ),
);

void _noop() {}

class _TestRecorder implements SpeechRecorder {
  @override
  Future<void> dispose() async {}

  @override
  Future<void> start() async {}

  @override
  Future<Uint8List> stop() async => Uint8List.fromList([1]);
}

class _QueuedSpeechClient implements SpeechClient {
  _QueuedSpeechClient(this._transcripts);

  final List<String> _transcripts;

  @override
  Future<String> transcribe(Uint8List bytes) async => _transcripts.removeAt(0);
}

Future<void> _pumpConfirmationFixture(
  WidgetTester tester,
  SpeechClient speechClient,
) async {
  await tester.pumpWidget(
    NestockApp(
      agentClient: FixtureAgentClient(confirmationFixtureReply),
      catalog: const DemoCatalog(),
      speechRecorder: _TestRecorder(),
      speechClient: speechClient,
    ),
  );
  final composer = find.descendant(
    of: find.byKey(const Key('composer')),
    matching: find.byType(TextField),
  );
  await tester.enterText(composer, '確認項目');
  await tester.tap(find.text('送出'));
  await tester.pumpAndSettle();
}

Future<void> _recordTranscript(WidgetTester tester) async {
  final gesture = await tester.startGesture(
    tester.getCenter(find.byTooltip('語音輸入')),
  );
  await tester.pump();
  await gesture.up();
  await tester.pumpAndSettle();
}

Future<void> _focusField(WidgetTester tester, Finder field) async {
  await tester.ensureVisible(field);
  await tester.pump();
  await tester.tap(field);
  await tester.pump();
}

void main() {
  testWidgets('resting card is readable, emphasized, and form-free', (
    tester,
  ) async {
    await tester.pumpWidget(_card());

    expect(find.text('確認加入庫存'), findsOneWidget);
    final name = tester.widget<Text>(find.text('高纖無糖豆漿'));
    expect(name.style?.fontWeight, FontWeight.w600);
    expect(find.text('數量　　1 瓶'), findsOneWidget);
    expect(find.text('冰箱'), findsOneWidget);
    expect(find.text('收納位置'), findsNothing);
    expect(find.byIcon(Icons.location_on_outlined), findsNWidgets(2));
    expect(find.byType(TextField), findsNothing);
    expect(find.byKey(const Key('confirmation-edit-0')), findsOneWidget);
    expect(find.byKey(const Key('confirmation-exclude-0')), findsOneWidget);
    expect(find.byType(Divider), findsNothing);
    for (final index in [0, 1]) {
      final block = tester.widget<Container>(
        find.byKey(Key('confirmation-item-block-$index')),
      );
      expect((block.decoration! as BoxDecoration).color, AppTokens.paleBlue);
    }
    final nameRect = tester.getRect(find.text('高纖無糖豆漿'));
    final quantity = tester.getRect(find.text('數量　　1 瓶'));
    expect(quantity.top - nameRect.bottom, lessThanOrEqualTo(8));
    expect(tester.takeException(), isNull);
  });

  testWidgets('explicit Confirmation cancel invokes its local callback', (
    tester,
  ) async {
    var cancellations = 0;
    await tester.pumpWidget(_card(onCancel: () => cancellations++));

    await tester.tap(find.byKey(const Key('confirmation-cancel')));

    expect(cancellations, 1);
  });

  testWidgets('outer close does not invoke the Confirmation cancel callback', (
    tester,
  ) async {
    var workOpen = true;
    var cancellations = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: StatefulBuilder(
          builder: (context, setState) => Scaffold(
            body: Column(
              children: [
                IconButton(
                  tooltip: '關閉工作區',
                  onPressed: () => setState(() => workOpen = false),
                  icon: const Icon(Icons.close),
                ),
                IgnorePointer(
                  ignoring: !workOpen,
                  child: Opacity(
                    opacity: workOpen ? 1 : 0,
                    child: ConfirmationCard(
                      items: _items,
                      onCancel: () => cancellations++,
                      onConfirm: _noop,
                    ),
                  ),
                ),
                if (!workOpen)
                  TextButton(
                    onPressed: () => setState(() => workOpen = true),
                    child: const Text('顯示互動'),
                  ),
              ],
            ),
          ),
        ),
      ),
    );

    await tester.tap(find.byTooltip('關閉工作區'));
    await tester.pump();
    expect(cancellations, 0);
    await tester.tap(find.text('顯示互動'));
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-cancel')));
    expect(cancellations, 1);
  });

  testWidgets('long Confirmation content tolerates realistic text scaling', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      _card(items: _longItems, textScaler: TextScaler.linear(1.3)),
    );
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();

    expect(find.text('特濃無糖高蛋白豆漿家庭分享包'), findsOneWidget);
    expect(find.text('大型家庭補充包'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets(
    'item block and edit affordance enter the same inline edit mode',
    (tester) async {
      await tester.pumpWidget(_card());

      await tester.tap(find.byKey(const Key('confirmation-edit-target-0')));
      await tester.pump();
      expect(
        find.byKey(const Key('confirmation-finish-edit-0')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('confirmation-name-0')), findsOneWidget);
      expect(find.byKey(const Key('confirmation-quantity-0')), findsOneWidget);
      expect(find.byKey(const Key('confirmation-location-0')), findsOneWidget);
      final nameField = tester.widget<TextField>(
        find.byKey(const Key('confirmation-name-0')),
      );
      final quantityField = tester.widget<TextField>(
        find.byKey(const Key('confirmation-quantity-0')),
      );
      final locationField = tester.widget<TextField>(
        find.byKey(const Key('confirmation-location-0')),
      );
      expect(nameField.style?.fontWeight, FontWeight.w600);
      expect(quantityField.style?.fontSize, locationField.style?.fontSize);
      expect(quantityField.style?.height, locationField.style?.height);

      await tester.tap(find.byKey(const Key('confirmation-finish-edit-0')));
      await tester.pump();
      await tester.tap(find.byKey(const Key('confirmation-edit-1')));
      await tester.pump();
      expect(
        find.byKey(const Key('confirmation-finish-edit-1')),
        findsOneWidget,
      );
      expect(find.byKey(const Key('confirmation-name-1')), findsOneWidget);
    },
  );

  testWidgets('local edits persist after finishing and unit remains fixed', (
    tester,
  ) async {
    await tester.pumpWidget(_card());
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();

    await tester.enterText(
      find.byKey(const Key('confirmation-name-0')),
      '無糖豆漿',
    );
    await tester.enterText(
      find.byKey(const Key('confirmation-quantity-0')),
      '3',
    );
    await tester.enterText(
      find.byKey(const Key('confirmation-location-0')),
      '冷藏櫃',
    );
    expect(find.text('瓶'), findsOneWidget);

    await tester.tap(find.byKey(const Key('confirmation-finish-edit-0')));
    await tester.pump();
    expect(find.byType(TextField), findsNothing);
    expect(find.text('無糖豆漿'), findsOneWidget);
    expect(find.text('數量　　3 瓶'), findsOneWidget);
    expect(find.text('冷藏櫃'), findsOneWidget);
    expect(find.text('衛生紙'), findsOneWidget);
  });

  testWidgets(
    'quantity accepts numeric drafts and restores the last valid value',
    (tester) async {
      await tester.pumpWidget(_card());
      await tester.tap(find.byKey(const Key('confirmation-edit-0')));
      await tester.pump();
      final quantity = find.byKey(const Key('confirmation-quantity-0'));

      await tester.enterText(quantity, '0');
      expect(tester.widget<TextField>(quantity).controller!.text, '0');
      await tester.enterText(quantity, '12');
      expect(tester.widget<TextField>(quantity).controller!.text, '12');
      await tester.enterText(quantity, '1.5');
      expect(tester.widget<TextField>(quantity).controller!.text, '1.5');
      expect(find.text('瓶'), findsOneWidget);

      await tester.enterText(quantity, 'abc');
      expect(tester.widget<TextField>(quantity).controller!.text, '1.5');
      await tester.enterText(quantity, '-1');
      expect(tester.widget<TextField>(quantity).controller!.text, '1.5');
      await tester.enterText(quantity, '1.2.3');
      expect(tester.widget<TextField>(quantity).controller!.text, '1.5');

      await tester.enterText(quantity, '');
      expect(tester.widget<TextField>(quantity).controller!.text, isEmpty);
      await tester.tap(find.byKey(const Key('confirmation-finish-edit-0')));
      await tester.pump();
      expect(find.text('數量　　1.5 瓶'), findsOneWidget);
    },
  );

  testWidgets('exclude and undo stay local without entering edit mode', (
    tester,
  ) async {
    await tester.pumpWidget(_card());

    await tester.tap(find.byKey(const Key('confirmation-exclude-0')));
    await tester.pump();
    expect(find.byKey(const Key('confirmation-include-0')), findsOneWidget);
    expect(
      find.byKey(const Key('confirmation-item-excluded-0')),
      findsOneWidget,
    );
    expect(find.byKey(const Key('confirmation-finish-edit-0')), findsNothing);
    expect(find.byType(TextField), findsNothing);

    await tester.tap(find.byKey(const Key('confirmation-include-0')));
    await tester.pump();
    expect(find.byKey(const Key('confirmation-exclude-0')), findsOneWidget);
    expect(
      find.byKey(const Key('confirmation-item-included-0')),
      findsOneWidget,
    );
    expect(find.byType(TextField), findsNothing);
  });

  testWidgets('item action targets are comfortable and do not overlap', (
    tester,
  ) async {
    await tester.pumpWidget(_card());
    final edit = tester.getRect(find.byKey(const Key('confirmation-edit-0')));
    final exclude = tester.getRect(
      find.byKey(const Key('confirmation-exclude-0')),
    );
    expect(edit.width, greaterThanOrEqualTo(44));
    expect(edit.height, greaterThanOrEqualTo(44));
    expect(exclude.width, greaterThanOrEqualTo(44));
    expect(exclude.height, greaterThanOrEqualTo(44));
    expect(edit.overlaps(exclude), isFalse);

    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();
    await tester.tap(find.byKey(const Key('confirmation-exclude-1')));
    await tester.pump();
    expect(find.byKey(const Key('confirmation-finish-edit-0')), findsOneWidget);
    expect(find.byKey(const Key('confirmation-include-1')), findsOneWidget);
    expect(find.byKey(const Key('confirmation-finish-edit-1')), findsNothing);
  });

  testWidgets('borders use the supplied assets, remain separate, and rotate', (
    tester,
  ) async {
    await tester.pumpWidget(_card());
    for (final key in const [
      Key('confirmation-border-top'),
      Key('confirmation-border-bottom'),
      Key('confirmation-border-left'),
      Key('confirmation-border-right'),
    ]) {
      expect(find.byKey(key), findsOneWidget);
    }
    for (final key in const [
      Key('confirmation-border-top'),
      Key('confirmation-border-bottom'),
    ]) {
      final image = tester.widget<Image>(
        find.descendant(of: find.byKey(key), matching: find.byType(Image)),
      );
      expect(
        (image.image as AssetImage).assetName,
        'assets/images/ui/confirmation_border_horizontal.png',
      );
    }
    for (final key in const [
      Key('confirmation-border-left'),
      Key('confirmation-border-right'),
    ]) {
      final image = tester.widget<Image>(
        find.descendant(of: find.byKey(key), matching: find.byType(Image)),
      );
      expect(
        (image.image as AssetImage).assetName,
        'assets/images/ui/confirmation_border_vertical.png',
      );
    }
    Rect borderRect(Key key) => tester.getRect(
      find.descendant(of: find.byKey(key), matching: find.byType(Image)),
    );
    final top = borderRect(const Key('confirmation-border-top'));
    final bottom = borderRect(const Key('confirmation-border-bottom'));
    final left = borderRect(const Key('confirmation-border-left'));
    final right = borderRect(const Key('confirmation-border-right'));
    expect(top.overlaps(left), isFalse);
    expect(top.overlaps(right), isFalse);
    expect(bottom.overlaps(left), isFalse);
    expect(bottom.overlaps(right), isFalse);
    for (final key in const [
      Key('confirmation-border-bottom-rotate'),
      Key('confirmation-border-right-rotate'),
    ]) {
      final transform = tester.widget<Transform>(find.byKey(key));
      expect(transform.transform.storage[0], closeTo(-1, .00001));
      expect(transform.transform.storage[5], closeTo(-1, .00001));
    }
  });

  testWidgets('narrow card remains usable while editing', (tester) async {
    await tester.binding.setSurfaceSize(const Size(320, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(_card());
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();

    final card = tester.getRect(
      find.byKey(const Key('confirmation-card-shell')),
    );
    for (final key in const [
      Key('confirmation-cancel'),
      Key('confirmation-confirm'),
      Key('confirmation-finish-edit-0'),
      Key('confirmation-exclude-0'),
    ]) {
      expect(tester.getRect(find.byKey(key)).overlaps(card), isTrue);
    }
    expect(tester.takeException(), isNull);
  });

  testWidgets('confirmation fixture is isolated from ordinary conversation', (
    tester,
  ) async {
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

    await tester.enterText(input, '家裡還有牛奶嗎？');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();
    expect(find.text('確認加入庫存'), findsNothing);

    await tester.pumpWidget(
      NestockApp(
        agentClient: FixtureAgentClient(confirmationFixtureReply),
        catalog: const DemoCatalog(),
      ),
    );
    final fixtureInput = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    await tester.enterText(fixtureInput, '確認項目');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();
    expect(find.text('確認加入庫存'), findsOneWidget);
    expect(find.text('高纖無糖豆漿'), findsOneWidget);
    expect(find.text('衛生紙'), findsOneWidget);
  });

  testWidgets('confirmation fixture renders without an HTTP shortcut', (
    tester,
  ) async {
    await tester.pumpWidget(
      NestockApp(
        agentClient: FixtureAgentClient(confirmationFixtureReply),
        catalog: const DemoCatalog(),
      ),
    );
    final input = find.descendant(
      of: find.byKey(const Key('composer')),
      matching: find.byType(TextField),
    );
    await tester.enterText(input, '確認項目');
    await tester.tap(find.text('送出'));
    await tester.pumpAndSettle();

    expect(find.text('確認加入庫存'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('speech uses the composer when no Confirmation field has focus', (
    tester,
  ) async {
    await _pumpConfirmationFixture(tester, _QueuedSpeechClient(['一般語音']));

    await _recordTranscript(tester);

    final composer = tester.widget<TextField>(
      find.descendant(
        of: find.byKey(const Key('composer')),
        matching: find.byType(TextField),
      ),
    );
    expect(composer.controller!.text, '一般語音');
  });

  testWidgets('focused Confirmation draft fields receive speech locally', (
    tester,
  ) async {
    await _pumpConfirmationFixture(
      tester,
      _QueuedSpeechClient(['新名稱', '3', '幫我查一下衛生紙', '客廳櫃']),
    );
    await tester.tap(find.byKey(const Key('confirmation-edit-0')));
    await tester.pump();

    await _focusField(tester, find.byKey(const Key('confirmation-name-0')));
    await _recordTranscript(tester);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('confirmation-name-0')))
          .controller!
          .text,
      '高纖無糖豆漿 新名稱',
    );

    await _focusField(tester, find.byKey(const Key('confirmation-quantity-0')));
    await _recordTranscript(tester);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('confirmation-quantity-0')))
          .controller!
          .text,
      '3',
    );
    expect(find.text('瓶'), findsOneWidget);

    await _recordTranscript(tester);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('confirmation-quantity-0')))
          .controller!
          .text,
      '3',
    );

    await _focusField(tester, find.byKey(const Key('confirmation-location-0')));
    await _recordTranscript(tester);
    expect(
      tester
          .widget<TextField>(find.byKey(const Key('confirmation-location-0')))
          .controller!
          .text,
      '冰箱 客廳櫃',
    );
  });

  testWidgets(
    'unfocused Confirmation editing never reuses a stale speech target',
    (tester) async {
      await _pumpConfirmationFixture(tester, _QueuedSpeechClient(['回到 Noki']));
      await tester.tap(find.byKey(const Key('confirmation-edit-0')));
      await tester.pump();
      await _focusField(tester, find.byKey(const Key('confirmation-name-0')));
      FocusManager.instance.primaryFocus?.unfocus();
      await tester.pump();

      await _recordTranscript(tester);

      expect(
        tester
            .widget<TextField>(find.byKey(const Key('confirmation-name-0')))
            .controller!
            .text,
        '高纖無糖豆漿',
      );
      final composer = tester.widget<TextField>(
        find.descendant(
          of: find.byKey(const Key('composer')),
          matching: find.byType(TextField),
        ),
      );
      expect(composer.controller!.text, '回到 Noki');
    },
  );

  testWidgets(
    'closing the Work Surface preserves the card without routing speech to it',
    (tester) async {
      await _pumpConfirmationFixture(tester, _QueuedSpeechClient(['回到 Noki']));
      await tester.tap(find.byKey(const Key('confirmation-edit-0')));
      await tester.pump();
      final name = find.byKey(const Key('confirmation-name-0'));
      await _focusField(tester, name);

      await tester.tap(find.byTooltip('關閉'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('work-surface')).hitTestable(), findsNothing);

      await _recordTranscript(tester);
      expect(tester.widget<TextField>(name).controller!.text, '高纖無糖豆漿');
      final composer = tester.widget<TextField>(
        find.descendant(
          of: find.byKey(const Key('composer')),
          matching: find.byType(TextField),
        ),
      );
      expect(composer.controller!.text, '回到 Noki');

      await tester.tap(find.text('顯示互動'));
      await tester.pumpAndSettle();
      expect(find.text('確認加入庫存'), findsOneWidget);
      expect(tester.widget<TextField>(name).controller!.text, '高纖無糖豆漿');
    },
  );
}
