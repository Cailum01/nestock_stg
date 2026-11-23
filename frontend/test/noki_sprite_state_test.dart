import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/presentation/home_scene.dart';

class _SequenceRandom implements math.Random {
  _SequenceRandom(this._values);

  final List<double> _values;
  var _index = 0;

  @override
  bool nextBool() => nextInt(2) == 0;

  @override
  double nextDouble() => _values[_index++ % _values.length];

  @override
  int nextInt(int max) => 0;
}

Widget _scene({
  bool isProcessing = false,
  double ambientThinkingChance = 0,
  double shopMovementChance = 0,
  Duration? ambientThinkingDuration,
  math.Random? random,
}) => MaterialApp(
  home: Scaffold(
    body: SizedBox(
      width: 390,
      height: 640,
      child: HomeScene(
        onInventory: () {},
        onShopping: () {},
        onBackgroundTap: () {},
        isProcessing: isProcessing,
        statusMessage: isProcessing ? '思考中...' : null,
        ambientThinkingChance: ambientThinkingChance,
        shopMovementChance: shopMovementChance,
        ambientThinkingDuration: ambientThinkingDuration,
        ambientRandom: random ?? math.Random(1),
      ),
    ),
  ),
);

String _assetName(WidgetTester tester, Key key) =>
    (tester.widget<Image>(find.byKey(key)).image as AssetImage).assetName;

void _expectSprite(WidgetTester tester, String assetPath) =>
    expect(_assetName(tester, const Key('noki-sprite-image')), assetPath);

void main() {
  testWidgets('idle uses the original Noki sprite', (tester) async {
    await tester.pumpWidget(_scene());

    _expectSprite(tester, 'assets/images/agent/noki_original.png');
    expect(
      tester
          .widget<Transform>(find.byKey(const Key('noki-idle-sprite')))
          .transform
          .storage[0],
      1,
    );
  });

  testWidgets('processing switches to Thinking and returns to Original', (
    tester,
  ) async {
    await tester.pumpWidget(_scene(isProcessing: true));

    expect(find.text('思考中...'), findsOneWidget);
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');

    await tester.pumpWidget(_scene());
    await tester.pump();

    expect(find.text('思考中...'), findsNothing);
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
  });

  testWidgets('ambient Thinking pauses without presenting processing status', (
    tester,
  ) async {
    Future<void> pumpScene(bool isProcessing) => tester.pumpWidget(
      _scene(
        isProcessing: isProcessing,
        ambientThinkingChance: 1,
        shopMovementChance: 0,
        ambientThinkingDuration: const Duration(seconds: 4),
      ),
    );

    await pumpScene(false);
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));
    expect(find.text('思考中...'), findsNothing);
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');

    final worldAnchor = tester.getRect(find.byKey(const Key('agent')));
    await pumpScene(true);
    await tester.pump();
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');
    expect(tester.getRect(find.byKey(const Key('agent'))), worldAnchor);

    await tester.pump(const Duration(seconds: 4));
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');

    await pumpScene(false);
    await tester.pump();
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
  });

  testWidgets('ambient Thinking reschedules idle chatter after it ends', (
    tester,
  ) async {
    await tester.pumpWidget(
      _scene(
        ambientThinkingChance: 1,
        shopMovementChance: 0,
        ambientThinkingDuration: const Duration(seconds: 1),
        random: _SequenceRandom([1, 0, 1]),
      ),
    );

    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');

    await tester.pump(const Duration(seconds: 1));
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
    await tester.pump(const Duration(seconds: 9));
    await tester.pump(const Duration(milliseconds: 999));
    await tester.pump(const Duration(milliseconds: 2));
    await tester.pump();

    expect(find.byKey(const Key('idle-noki-bubble')), findsOneWidget);
  });

  testWidgets('opposite native sprite orientations preserve facing direction', (
    tester,
  ) async {
    await tester.pumpWidget(_scene());
    final sprite = find.byKey(const Key('noki-idle-sprite'));

    // Noki begins facing left: Original uses its native orientation, whereas
    // Thinking mirrors its right-facing native artwork.
    expect(tester.widget<Transform>(sprite).transform.storage[0], 1);
    await tester.pumpWidget(_scene(isProcessing: true));
    await tester.pump();
    expect(tester.widget<Transform>(sprite).transform.storage[0], -1);
    expect(
      tester
          .widget<Transform>(find.byKey(const Key('noki-sprite-ground-offset')))
          .transform
          .storage[13],
      greaterThan(0),
    );

    // Resume, then reach the next leg, where Noki faces right.
    await tester.pumpWidget(_scene());
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(seconds: 5));
    await tester.pump(const Duration(milliseconds: 200));
    expect(tester.widget<Transform>(sprite).transform.storage[0], -1);

    await tester.pumpWidget(_scene(isProcessing: true));
    await tester.pump();
    expect(tester.widget<Transform>(sprite).transform.storage[0], 1);
  });

  testWidgets('Shop is chosen at a boundary and lasts one movement segment', (
    tester,
  ) async {
    final random = _SequenceRandom([0, 1, 1]);
    await tester.pumpWidget(_scene(shopMovementChance: .22, random: random));

    _expectSprite(tester, 'assets/images/agent/noki_original.png');
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));

    // The first segment has ended. Shop is chosen during the normal endpoint
    // pause, before the following movement starts.
    _expectSprite(tester, 'assets/images/agent/noki_shop.png');
    expect(find.text('思考中...'), findsNothing);
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 2500));
    _expectSprite(tester, 'assets/images/agent/noki_shop.png');

    await tester.pump(const Duration(milliseconds: 2499));
    await tester.pump(const Duration(milliseconds: 3));
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
  });

  testWidgets('processing interrupts Shop and resumes with Original', (
    tester,
  ) async {
    final random = _SequenceRandom([0, 1, 1]);
    Future<void> pumpScene(bool isProcessing) => tester.pumpWidget(
      _scene(
        isProcessing: isProcessing,
        shopMovementChance: .22,
        random: random,
      ),
    );

    await pumpScene(false);
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(milliseconds: 4999));
    await tester.pump(const Duration(milliseconds: 3));
    await tester.pump(const Duration(milliseconds: 200));
    await tester.pump(const Duration(milliseconds: 2500));
    _expectSprite(tester, 'assets/images/agent/noki_shop.png');
    final worldAnchor = tester.getRect(find.byKey(const Key('agent')));

    await pumpScene(true);
    await tester.pump();
    _expectSprite(tester, 'assets/images/agent/noki_thinking.png');
    expect(tester.getRect(find.byKey(const Key('agent'))), worldAnchor);

    await pumpScene(false);
    await tester.pump();
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
    await tester.pump(const Duration(seconds: 4));
    _expectSprite(tester, 'assets/images/agent/noki_original.png');
  });
}
