import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/shopping/shopping_overlay.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';

class Fixture implements ShoppingViewClient {
  Fixture(this.count);
  final int count;
  @override
  Future<List<ShoppingViewItem>> load() async => List.generate(
    count,
    (i) => ShoppingViewItem('物品$i', '1', '包', 'pending'),
  );
}

void main() {
  testWidgets('empty and long shopping lists remain bounded', (tester) async {
    Widget panel(int count) => MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.topRight,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 320, maxHeight: 450),
            child: ShoppingOverlay(
              key: ValueKey(count),
              client: Fixture(count),
              onClose: () {},
            ),
          ),
        ),
      ),
    );
    await tester.pumpWidget(panel(0));
    await tester.pumpAndSettle();
    expect(find.text('購物清單目前是空的。'), findsOneWidget);
    await tester.pumpWidget(panel(30));
    await tester.pumpAndSettle();
    expect(
      tester.getSize(find.byKey(const Key('shopping-overlay'))).height,
      lessThanOrEqualTo(450),
    );
    await tester.drag(
      find.byKey(const Key('shopping-items')),
      const Offset(0, -500),
    );
    await tester.pumpAndSettle();
    expect(find.text('物品0'), findsNothing);
    expect(find.text('待購 30 件'), findsOneWidget);
  });
}
