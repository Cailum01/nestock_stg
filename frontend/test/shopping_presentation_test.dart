import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/shopping/shopping_overlay.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';
import 'package:nestock/shared/theme/app_theme.dart';

class _ShoppingFixture implements ShoppingViewClient {
  const _ShoppingFixture(this.items);

  final List<ShoppingViewItem> items;

  @override
  Future<List<ShoppingViewItem>> load() async => items;
}

class _FailingShoppingClient implements ShoppingViewClient {
  @override
  Future<List<ShoppingViewItem>> load() async => throw StateError('offline');
}

class _LoadingShoppingClient implements ShoppingViewClient {
  final result = Completer<List<ShoppingViewItem>>();

  @override
  Future<List<ShoppingViewItem>> load() => result.future;
}

Widget _overlay(ShoppingViewClient client) => MaterialApp(
  theme: buildAppTheme(),
  home: Scaffold(
    body: SizedBox(
      width: 360,
      height: 500,
      child: ShoppingOverlay(
        key: ValueKey(client),
        client: client,
        onClose: () {},
      ),
    ),
  ),
);

void main() {
  testWidgets('Shopping gives each item its own themed compact surface', (
    tester,
  ) async {
    await tester.pumpWidget(
      _overlay(
        const _ShoppingFixture([
          ShoppingViewItem(name: '洗衣精', status: 'pending'),
          ShoppingViewItem(name: '衛生紙', status: 'pending'),
        ]),
      ),
    );
    await tester.pumpAndSettle();

    final expectedColor = buildAppTheme().colorScheme.surfaceContainerHighest;
    final title = tester.widget<Text>(
      find.byKey(const Key('shopping-overlay-title')),
    );
    expect(title.style?.fontWeight, FontWeight.w600);
    expect(find.text('待購 2 件'), findsOneWidget);
    expect(
      tester
          .widget<Align>(find.byKey(const Key('shopping-pending-count')))
          .alignment,
      Alignment.centerLeft,
    );
    for (var index = 0; index < 2; index++) {
      final surface = find.byKey(Key('shopping-item-surface-$index'));
      expect(surface, findsOneWidget);
      expect(tester.widget<ColoredBox>(surface).color, expectedColor);
    }
    expect(find.byKey(const Key('shopping-item-separator')), findsOneWidget);
    expect(
      tester.getSize(find.byKey(const Key('shopping-item-separator'))).height,
      2,
    );
    expect(find.byKey(const Key('shopping-procure-洗衣精')), findsOneWidget);
    expect(find.byKey(const Key('shopping-procure-衛生紙')), findsOneWidget);
    expect(find.byType(Card), findsNothing);
  });

  testWidgets('Shopping loading error and empty states remain available', (
    tester,
  ) async {
    final loading = _LoadingShoppingClient();
    await tester.pumpWidget(_overlay(loading));
    await tester.pump();
    expect(find.byType(CircularProgressIndicator), findsOneWidget);

    loading.result.complete(const []);
    await tester.pumpAndSettle();
    expect(find.text('購物清單目前是空的。'), findsOneWidget);
    expect(find.byKey(const Key('shopping-add-entry')), findsOneWidget);

    await tester.pumpWidget(_overlay(_FailingShoppingClient()));
    await tester.pumpAndSettle();
    expect(find.text('無法載入購物清單。'), findsOneWidget);
  });
}
