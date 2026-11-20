import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/shopping/shopping_overlay.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class _PersistentShoppingClient
    implements ShoppingViewClient, ShoppingManualAddClient {
  final List<ShoppingViewItem> savedItems = [];
  final List<({String requestId, String name, int? quantity, String? unit})>
  requests = [];
  int loadCount = 0;
  bool failFirstAdd = true;

  @override
  Future<List<ShoppingViewItem>> load() async {
    loadCount++;
    return List.unmodifiable(savedItems);
  }

  @override
  Future<ShoppingViewItem> add({
    required String requestId,
    required String name,
    int? quantity,
    String? unit,
  }) async {
    requests.add((
      requestId: requestId,
      name: name,
      quantity: quantity,
      unit: unit,
    ));
    if (failFirstAdd) {
      failFirstAdd = false;
      throw StateError('temporary failure');
    }
    final item = ShoppingViewItem(
      name: name,
      quantity: quantity,
      unit: unit,
      status: 'pending',
    );
    savedItems.add(item);
    return item;
  }
}

class _ShoppingHost extends StatefulWidget {
  const _ShoppingHost({required this.client});

  final ShoppingViewClient client;

  @override
  State<_ShoppingHost> createState() => _ShoppingHostState();
}

class _ShoppingHostState extends State<_ShoppingHost> {
  bool _open = false;

  @override
  Widget build(BuildContext context) => MaterialApp(
    home: Scaffold(
      appBar: AppBar(
        actions: [
          if (_open)
            IconButton(
              key: const Key('shopping-close-host'),
              onPressed: () => setState(() => _open = false),
              icon: const Icon(Icons.close),
            )
          else
            IconButton(
              key: const Key('shopping-reopen'),
              tooltip: 'Open Shopping',
              onPressed: () => setState(() => _open = true),
              icon: const Icon(Icons.shopping_cart),
            ),
        ],
      ),
      body: Align(
        alignment: Alignment.topRight,
        child: _open
            ? ConstrainedBox(
                constraints: const BoxConstraints(
                  maxWidth: 360,
                  maxHeight: 450,
                ),
                child: ShoppingOverlay(
                  client: widget.client,
                  onClose: () => setState(() => _open = false),
                ),
              )
            : const SizedBox.shrink(),
      ),
    ),
  );
}

void main() {
  testWidgets('manual Add retries idempotently and reloads after reopening', (
    tester,
  ) async {
    final shoppingClient = _PersistentShoppingClient();
    await tester.pumpWidget(_ShoppingHost(client: shoppingClient));
    await tester.tap(find.byKey(const Key('shopping-reopen')));
    await tester.pumpAndSettle();
    expect(shoppingClient.loadCount, 1);
    expect(shoppingClient.requests, isEmpty);

    await tester.tap(find.byKey(const Key('shopping-add-entry')));
    await tester.pumpAndSettle();
    await tester.enterText(find.byKey(const Key('shopping-add-name')), '豆漿');
    await tester.enterText(find.byKey(const Key('shopping-add-quantity')), '2');
    await tester.enterText(find.byKey(const Key('shopping-add-unit')), '瓶');
    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();
    expect(find.text('無法儲存購物項目，請重試。'), findsOneWidget);
    expect(shoppingClient.requests, hasLength(1));
    expect(shoppingClient.savedItems, isEmpty);

    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();
    expect(shoppingClient.requests, hasLength(2));
    expect(
      shoppingClient.requests[1].requestId,
      shoppingClient.requests[0].requestId,
    );
    expect(shoppingClient.requests[1].name, '豆漿');
    expect(shoppingClient.requests[1].quantity, 2);
    expect(shoppingClient.requests[1].unit, '瓶');
    expect(find.text('無法儲存購物項目，請重試。'), findsNothing);
    expect(
      find.descendant(
        of: find.byKey(const Key('shopping-items')),
        matching: find.text('豆漿'),
      ),
      findsOneWidget,
    );
    expect(shoppingClient.loadCount, 2);
    expect(find.byKey(const Key('shopping-add-name')), findsNothing);

    tester.testTextInput.hide();
    tester.binding.focusManager.primaryFocus?.unfocus();
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('關閉購物清單'));
    await tester.pumpAndSettle();
    expect(find.byKey(const Key('shopping-overlay')), findsNothing);
    await tester.tap(find.byKey(const Key('shopping-reopen')));
    await tester.pumpAndSettle();

    expect(shoppingClient.loadCount, 3);
    expect(find.text('豆漿'), findsOneWidget);
    expect(shoppingClient.savedItems.single.status, 'pending');
    expect(tester.takeException(), isNull);
  });

  for (final size in [const Size(320, 568), const Size(390, 844)]) {
    testWidgets('shopping and inventory coordinate at $size', (tester) async {
      tester.view.physicalSize = size;
      tester.view.devicePixelRatio = 1;
      addTearDown(tester.view.resetPhysicalSize);
      addTearDown(tester.view.resetDevicePixelRatio);
      await tester.pumpWidget(
        NestockApp(
          agentClient: MockAgentClient(),
          catalog: const DemoCatalog(),
        ),
      );
      await tester.tap(find.byKey(const Key('cart')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
      expect(find.text('牛奶'), findsOneWidget);
      expect(find.byKey(const Key('composer')), findsOneWidget);
      expect(find.byKey(const Key('paw-send')).hitTestable(), findsOneWidget);
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cabinet')));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(find.text('我的庫存'), findsOneWidget);
      await tester.tap(find.byTooltip('關閉庫存'));
      await tester.pumpAndSettle();
      await tester.tap(find.byKey(const Key('cart')));
      await tester.pumpAndSettle();
      expect(find.text('我的庫存'), findsNothing);
      expect(find.byKey(const Key('shopping-overlay')), findsOneWidget);
      await tester.tap(find.byTooltip('關閉購物清單'));
      await tester.pumpAndSettle();
      expect(find.byKey(const Key('shopping-overlay')), findsNothing);
      expect(tester.takeException(), isNull);
    });
  }
}
