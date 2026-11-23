import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/inventory/inventory_overlay.dart';
import 'package:nestock/features/inventory/inventory_view_client.dart';
import 'package:nestock/shared/theme/app_theme.dart';

class _InventoryFixture implements InventoryViewClient {
  const _InventoryFixture(this.items);

  final List<InventoryViewItem> items;

  @override
  Future<List<InventoryViewItem>> load() async => items;
}

class _FailingInventoryClient implements InventoryViewClient {
  @override
  Future<List<InventoryViewItem>> load() async => throw StateError('offline');
}

class _LoadingInventoryClient implements InventoryViewClient {
  final result = Completer<List<InventoryViewItem>>();

  @override
  Future<List<InventoryViewItem>> load() => result.future;
}

final _items = [
  InventoryViewItem(
    'Zebra',
    '1',
    '個',
    aliases: ['matched'],
    location: ' Pantry ',
    expiry: DateTime(2026, 12, 10),
  ),
  InventoryViewItem('Mango', '2', '個'),
  InventoryViewItem(
    'apple',
    '3',
    '個',
    aliases: ['matched'],
    location: 'bathroom',
    expiry: DateTime(2026, 12, 1),
  ),
  InventoryViewItem(
    'Banana',
    '4',
    '個',
    location: 'Attic',
    expiry: DateTime(2026, 12, 1),
  ),
];

Widget _overlay(InventoryViewClient client) => MaterialApp(
  theme: buildAppTheme(),
  home: Scaffold(
    body: SizedBox(
      width: 360,
      height: 640,
      child: InventoryOverlay(
        key: ValueKey(client),
        client: client,
        onClose: () {},
      ),
    ),
  ),
);

List<String> _visibleOrder(WidgetTester tester, Iterable<String> names) {
  final positioned = [
    for (final name in names)
      (name: name, top: tester.getTopLeft(find.text(name)).dy),
  ]..sort((left, right) => left.top.compareTo(right.top));
  return positioned.map((item) => item.name).toList(growable: false);
}

Future<void> _selectSort(WidgetTester tester, String label) async {
  await tester.tap(find.byKey(const Key('inventory-sort')));
  await tester.pumpAndSettle();
  await tester.tap(find.text(label).last);
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('Inventory gives each item its own themed compact surface', (
    tester,
  ) async {
    await tester.pumpWidget(_overlay(_InventoryFixture(_items)));
    await tester.pumpAndSettle();

    final expectedColor = buildAppTheme().colorScheme.surfaceContainerHighest;
    final title = tester.widget<Text>(
      find.byKey(const Key('inventory-overlay-title')),
    );
    expect(title.style?.fontWeight, FontWeight.w600);
    expect(find.text('顯示 4 件'), findsOneWidget);
    expect(find.byKey(const Key('inventory-summary-row')), findsOneWidget);
    expect(
      tester
          .getSize(find.byKey(const Key('inventory-summary-list-gap')))
          .height,
      8,
    );
    for (var index = 0; index < _items.length; index++) {
      final surface = find.byKey(Key('inventory-item-surface-$index'));
      expect(surface, findsOneWidget);
      expect(tester.widget<ColoredBox>(surface).color, expectedColor);
    }
    expect(
      find.byKey(const Key('inventory-item-separator')),
      findsNWidgets(_items.length - 1),
    );
    expect(
      tester
          .getSize(find.byKey(const Key('inventory-item-separator')).first)
          .height,
      2,
    );
    expect(find.byType(Card), findsNothing);
  });

  testWidgets('Inventory default mode preserves client source order', (
    tester,
  ) async {
    await tester.pumpWidget(_overlay(_InventoryFixture(_items)));
    await tester.pumpAndSettle();

    expect(_visibleOrder(tester, _items.map((item) => item.name)), [
      'Zebra',
      'Mango',
      'apple',
      'Banana',
    ]);
  });

  testWidgets('Inventory sorts locations with missing locations last', (
    tester,
  ) async {
    await tester.pumpWidget(_overlay(_InventoryFixture(_items)));
    await tester.pumpAndSettle();
    await _selectSort(tester, '位置');

    expect(_visibleOrder(tester, _items.map((item) => item.name)), [
      'Banana',
      'apple',
      'Zebra',
      'Mango',
    ]);
  });

  testWidgets(
    'Inventory sorts earliest expiry first with missing expiry last',
    (tester) async {
      await tester.pumpWidget(_overlay(_InventoryFixture(_items)));
      await tester.pumpAndSettle();
      await _selectSort(tester, '效期');

      expect(_visibleOrder(tester, _items.map((item) => item.name)), [
        'apple',
        'Banana',
        'Zebra',
        'Mango',
      ]);
      expect(find.text('效期 2026/12/01'), findsNWidgets(2));
    },
  );

  testWidgets('Inventory sorts names and composes sorting with search', (
    tester,
  ) async {
    await tester.pumpWidget(_overlay(_InventoryFixture(_items)));
    await tester.pumpAndSettle();
    await _selectSort(tester, '名稱');

    expect(_visibleOrder(tester, _items.map((item) => item.name)), [
      'apple',
      'Banana',
      'Mango',
      'Zebra',
    ]);

    await tester.enterText(
      find.byKey(const Key('inventory-search')),
      'matched',
    );
    await tester.pumpAndSettle();
    expect(_visibleOrder(tester, ['Zebra', 'apple']), ['apple', 'Zebra']);
  });

  testWidgets('Inventory loading error and empty states remain available', (
    tester,
  ) async {
    final loading = _LoadingInventoryClient();
    await tester.pumpWidget(_overlay(loading));
    await tester.pump();
    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    loading.result.complete(const []);
    await tester.pumpAndSettle();
    expect(find.text('目前沒有庫存品項。'), findsOneWidget);

    await tester.pumpWidget(_overlay(_FailingInventoryClient()));
    await tester.pumpAndSettle();
    expect(find.text('無法載入庫存。'), findsOneWidget);
  });
}
