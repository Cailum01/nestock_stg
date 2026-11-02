import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/inventory/inventory_overlay.dart';
import 'package:nestock/features/inventory/inventory_view_client.dart';

class EmptyClient implements InventoryViewClient {
  @override
  Future<List<InventoryViewItem>> load() async => [];
}

void main() {
  testWidgets('empty inventory is contained in overlay', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            height: 400,
            child: InventoryOverlay(client: EmptyClient(), onClose: () {}),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    expect(find.text('目前沒有庫存品項。'), findsOneWidget);
    expect(find.text('顯示 0 件'), findsOneWidget);
  });
  testWidgets('name search and optional expiry presentation', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: SizedBox(
            height: 500,
            child: InventoryOverlay(
              client: const DemoInventoryViewClient(),
              onClose: () {},
            ),
          ),
        ),
      ),
    );
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('inventory-search')), '咖啡');
    await tester.pumpAndSettle();
    expect(find.text('效期 2026/11/13'), findsOneWidget);
    await tester.enterText(find.byKey(const Key('inventory-search')), '衛生紙');
    await tester.pumpAndSettle();
    expect(find.byIcon(Icons.calendar_today_outlined), findsNothing);
    await tester.enterText(find.byKey(const Key('inventory-search')), '儲藏櫃');
    await tester.pumpAndSettle();
    expect(find.text('找不到符合的物品。'), findsOneWidget);
  });
}
