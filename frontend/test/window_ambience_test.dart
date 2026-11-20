import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/presentation/home_scene.dart';

DateTime _localTime(int hour, int minute) =>
    DateTime(2026, 11, 19, hour, minute);

Widget _scene({
  required DateTime Function() now,
  required WindowAmbienceAssetSelector selectAsset,
  required Duration checkInterval,
  bool workSurfaceOpen = false,
  String? statusMessage,
}) => MaterialApp(
  home: Scaffold(
    body: SizedBox(
      width: 390,
      height: 640,
      child: HomeScene(
        onInventory: () {},
        onShopping: () {},
        onBackgroundTap: () {},
        localTime: now,
        selectAmbienceAsset: selectAsset,
        ambienceCheckInterval: checkInterval,
        workSurfaceOpen: workSurfaceOpen,
        statusMessage: statusMessage,
      ),
    ),
  ),
);

String _selectedAsset(WidgetTester tester) =>
    (tester.widget<Image>(find.byKey(const Key('window-ambience-image'))).image
            as AssetImage)
        .assetName;

void main() {
  test('local time resolves to the intended ambience periods', () {
    expect(
      resolveWindowAmbiencePeriod(_localTime(5, 59)),
      WindowAmbiencePeriod.night,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(6, 0)),
      WindowAmbiencePeriod.dawn,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(8, 59)),
      WindowAmbiencePeriod.dawn,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(9, 0)),
      WindowAmbiencePeriod.day,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(16, 59)),
      WindowAmbiencePeriod.day,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(17, 0)),
      WindowAmbiencePeriod.dusk,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(18, 59)),
      WindowAmbiencePeriod.dusk,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(19, 0)),
      WindowAmbiencePeriod.night,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(23, 30)),
      WindowAmbiencePeriod.night,
    );
    expect(
      resolveWindowAmbiencePeriod(_localTime(2, 0)),
      WindowAmbiencePeriod.night,
    );
  });

  test('asset selection stays inside each ambience pool', () {
    for (final period in WindowAmbiencePeriod.values) {
      final candidates = windowAmbienceAssets[period]!;
      for (var seed = 0; seed < 8; seed++) {
        expect(
          candidates,
          contains(
            selectWindowAmbienceAsset(period, random: math.Random(seed)),
          ),
        );
      }
    }
  });

  testWidgets(
    'ambience retains its asset across rebuilds and changes only at a boundary',
    (tester) async {
      var localTime = _localTime(10, 0);
      var selectionCount = 0;
      DateTime now() => localTime;
      String selectAsset(WindowAmbiencePeriod period) {
        selectionCount += 1;
        return windowAmbienceAssets[period]!.last;
      }

      await tester.pumpWidget(
        _scene(
          now: now,
          selectAsset: selectAsset,
          checkInterval: const Duration(minutes: 1),
        ),
      );
      final window = tester.getRect(find.byKey(const Key('window')));
      final viewport = tester.getRect(
        find.byKey(const Key('weather-viewport')),
      );
      expect(_selectedAsset(tester), 'assets/images/weather/day_04.png');
      expect(selectionCount, 1);
      expect(viewport.left, closeTo(window.left + window.width * .15, .1));
      expect(viewport.top, closeTo(window.top + window.height * .30, .1));
      expect(viewport.width, closeTo(window.width * .70, .1));
      expect(viewport.height, closeTo(window.height * .55, .1));

      await tester.pumpWidget(
        _scene(
          now: now,
          selectAsset: selectAsset,
          checkInterval: const Duration(minutes: 1),
          statusMessage: '思考中...',
        ),
      );
      expect(_selectedAsset(tester), 'assets/images/weather/day_04.png');
      expect(selectionCount, 1);

      await tester.pump(const Duration(minutes: 1));
      expect(_selectedAsset(tester), 'assets/images/weather/day_04.png');
      expect(selectionCount, 1);

      await tester.pumpWidget(
        _scene(
          now: now,
          selectAsset: selectAsset,
          checkInterval: const Duration(minutes: 1),
          workSurfaceOpen: true,
        ),
      );
      expect(find.byKey(const Key('window-ambience-image')), findsNothing);
      expect(selectionCount, 1);

      await tester.pumpWidget(
        _scene(
          now: now,
          selectAsset: selectAsset,
          checkInterval: const Duration(minutes: 1),
        ),
      );
      expect(_selectedAsset(tester), 'assets/images/weather/day_04.png');
      expect(selectionCount, 1);

      localTime = _localTime(17, 0);
      await tester.pump(const Duration(minutes: 1));
      expect(_selectedAsset(tester), 'assets/images/weather/dusk_01.png');
      expect(selectionCount, 2);

      await tester.pumpWidget(
        _scene(
          now: now,
          selectAsset: selectAsset,
          checkInterval: const Duration(minutes: 1),
          statusMessage: '思考中...',
        ),
      );
      expect(_selectedAsset(tester), 'assets/images/weather/dusk_01.png');
      expect(selectionCount, 2);
    },
  );
}
