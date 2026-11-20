import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/app.dart';
import 'package:nestock/features/agent/data/mock_agent_client.dart';
import 'package:nestock/features/shopping/shopping_overlay.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';
import 'package:nestock/shared/data/demo_catalog.dart';

class Fixture implements ShoppingViewClient {
  Fixture(this.count, {this.namePrefix = '物品'});
  final int count;
  final String namePrefix;
  final addedItems = <ShoppingViewItem>[];
  final addRequests = <AddShoppingItemRequest>[];
  bool failNextAdd = false;

  @override
  Future<List<ShoppingViewItem>> load() async => List.generate(
    count,
    (i) => ShoppingViewItem(
      name: '$namePrefix$i',
      quantity: 1,
      unit: '包',
      status: 'pending',
    ),
  )..addAll(addedItems);

  @override
  Future<ShoppingViewItem> add(AddShoppingItemRequest request) async {
    addRequests.add(request);
    if (failNextAdd) {
      failNextAdd = false;
      throw const ShoppingAddException(ShoppingAddFailure.timeout);
    }
    final item = ShoppingViewItem(name: request.name, status: 'pending');
    addedItems.add(item);
    return item;
  }
}

class NameOnlyFixture implements ShoppingViewClient {
  const NameOnlyFixture();

  @override
  Future<List<ShoppingViewItem>> load() async => const [
    ShoppingViewItem(name: '豆漿', status: 'pending'),
  ];

  @override
  Future<ShoppingViewItem> add(AddShoppingItemRequest request) =>
      throw const ShoppingAddException(ShoppingAddFailure.unavailable);
}

Widget panel({
  int count = 2,
  String namePrefix = '物品',
  VoidCallback? onClose,
  double? maxHeight,
  TextScaler? textScaler,
}) => MaterialApp(
  builder: textScaler == null
      ? null
      : (context, child) => MediaQuery(
          data: MediaQuery.of(context).copyWith(textScaler: textScaler),
          child: child!,
        ),
  home: Scaffold(
    body: Align(
      alignment: Alignment.topRight,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: 320, maxHeight: maxHeight ?? 450),
        child: ShoppingOverlay(
          client: Fixture(count, namePrefix: namePrefix),
          onClose: onClose ?? () {},
        ),
      ),
    ),
  ),
);

Future<void> openAdd(WidgetTester tester) async {
  await tester.tap(find.byKey(const Key('shopping-add-entry')));
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('existing item names render without quantity or unit', (
    tester,
  ) async {
    await tester.pumpWidget(panel());
    await tester.pumpAndSettle();

    expect(find.text('物品0'), findsOneWidget);
    expect(find.text('物品1'), findsOneWidget);
    expect(find.text('1 包'), findsNothing);
    expect(find.text('待購買'), findsNothing);
    expect(find.text('新增購物項目'), findsOneWidget);
  });

  testWidgets('empty shopping list still exposes the add entry point', (
    tester,
  ) async {
    await tester.pumpWidget(panel(count: 0));
    await tester.pumpAndSettle();

    expect(find.text('購物清單目前是空的。'), findsOneWidget);
    expect(find.byKey(const Key('shopping-add-entry')), findsOneWidget);
  });

  testWidgets('name-only server items do not show a fake quantity or unit', (
    tester,
  ) async {
    await tester.pumpWidget(
      MaterialApp(
        home: ShoppingOverlay(client: const NameOnlyFixture(), onClose: () {}),
      ),
    );
    await tester.pumpAndSettle();

    expect(find.text('豆漿'), findsOneWidget);
    expect(find.text('1'), findsNothing);
    expect(find.text('個'), findsNothing);
  });

  testWidgets('add form contains only item name and cancel closes it', (
    tester,
  ) async {
    await tester.pumpWidget(panel());
    await tester.pumpAndSettle();
    await openAdd(tester);

    expect(find.byKey(const Key('shopping-add-name')), findsOneWidget);
    expect(find.byKey(const Key('shopping-add-quantity')), findsNothing);
    expect(find.text('數量'), findsNothing);
    await tester.enterText(find.byKey(const Key('shopping-add-name')), '豆漿');
    await tester.tap(find.text('取消'));
    await tester.pumpAndSettle();

    expect(find.byKey(const Key('shopping-add-name')), findsNothing);
    expect(find.text('豆漿'), findsNothing);
    expect(find.byKey(const Key('shopping-add-entry')), findsOneWidget);
  });

  testWidgets('valid item name is persisted through the Shopping client', (
    tester,
  ) async {
    final client = Fixture(0);
    await tester.pumpWidget(
      MaterialApp(
        home: ShoppingOverlay(
          client: client,
          onClose: () {},
          createRequestId: () => 'stable-request',
        ),
      ),
    );
    await tester.pumpAndSettle();
    await openAdd(tester);
    await tester.enterText(
      find.byKey(const Key('shopping-add-name')),
      '  豆漿  ',
    );
    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();

    expect(find.text('豆漿'), findsOneWidget);
    expect(find.text('1'), findsNothing);
    expect(find.text('瓶'), findsNothing);
    expect(find.byKey(const Key('shopping-add-name')), findsNothing);
    expect(client.addRequests, hasLength(1));
    expect(client.addRequests.single.requestId, 'stable-request');
  });

  testWidgets('retry reuses the same request id after a timeout', (
    tester,
  ) async {
    final client = Fixture(0)..failNextAdd = true;
    var requestIdsCreated = 0;
    await tester.pumpWidget(
      MaterialApp(
        home: ShoppingOverlay(
          client: client,
          onClose: () {},
          createRequestId: () {
            requestIdsCreated += 1;
            return 'retryable-request';
          },
        ),
      ),
    );
    await tester.pumpAndSettle();
    await openAdd(tester);
    await tester.enterText(find.byKey(const Key('shopping-add-name')), '豆漿');

    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();
    expect(find.text('連線逾時，請重試。'), findsOneWidget);

    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();

    expect(client.addRequests, hasLength(2));
    expect(client.addRequests.map((request) => request.requestId).toSet(), {
      'retryable-request',
    });
    expect(requestIdsCreated, 1);
    expect(find.text('豆漿'), findsOneWidget);
  });

  testWidgets('blank item name is rejected', (tester) async {
    await tester.pumpWidget(panel());
    await tester.pumpAndSettle();
    await openAdd(tester);
    await tester.enterText(find.byKey(const Key('shopping-add-name')), '   ');
    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();

    expect(find.text('請輸入商品名稱。'), findsOneWidget);
    expect(find.byKey(const Key('shopping-add-name')), findsOneWidget);
  });

  testWidgets('shopping add area remains bounded on a narrow viewport', (
    tester,
  ) async {
    tester.view.physicalSize = const Size(320, 568);
    tester.view.devicePixelRatio = 1;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);
    await tester.pumpWidget(panel(count: 30, maxHeight: 450));
    await tester.pumpAndSettle();
    await openAdd(tester);

    expect(
      tester.getSize(find.byKey(const Key('shopping-overlay'))).height,
      lessThanOrEqualTo(450),
    );
    expect(tester.takeException(), isNull);
  });

  testWidgets('long Shopping content tolerates realistic text scaling', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 800));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(
      panel(namePrefix: '家庭常備無糖高蛋白豆漿補充組', textScaler: TextScaler.linear(1.3)),
    );
    await tester.pumpAndSettle();
    await openAdd(tester);
    await tester.enterText(
      find.byKey(const Key('shopping-add-name')),
      '特濃無糖豆漿家庭分享包',
    );
    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();

    expect(find.text('特濃無糖豆漿家庭分享包'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });

  testWidgets('persisted Shopping items reload when the overlay is reopened', (
    tester,
  ) async {
    final client = Fixture(0);
    await tester.pumpWidget(
      NestockApp(
        agentClient: MockAgentClient(),
        catalog: const DemoCatalog(),
        shoppingClient: client,
      ),
    );
    await tester.tap(find.byKey(const Key('cart')));
    await tester.pumpAndSettle();
    await openAdd(tester);
    await tester.enterText(find.byKey(const Key('shopping-add-name')), '豆漿');
    await tester.tap(find.text('加入'));
    await tester.pumpAndSettle();
    expect(find.text('豆漿'), findsOneWidget);

    await tester.tap(find.byTooltip('關閉購物清單'));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const Key('cart')));
    await tester.pumpAndSettle();

    expect(find.text('豆漿'), findsOneWidget);
    expect(find.text('購物清單目前是空的。'), findsNothing);
  });

  testWidgets('close action remains functional', (tester) async {
    var closed = false;
    await tester.pumpWidget(panel(onClose: () => closed = true));
    await tester.pumpAndSettle();
    await tester.tap(find.byTooltip('關閉購物清單'));

    expect(closed, isTrue);
  });
}
