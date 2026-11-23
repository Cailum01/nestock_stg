import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/shopping/commerce_options_client.dart';
import 'package:nestock/features/shopping/shopping_overlay.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';

class _ShoppingFixture implements ShoppingViewClient {
  const _ShoppingFixture(this.name);

  final String name;

  @override
  Future<List<ShoppingViewItem>> load() async => [
    ShoppingViewItem(name: name, status: 'pending'),
  ];
}

class _CommerceFixture implements CommerceOptionsClient {
  _CommerceFixture(this.response);

  final Future<CommerceOptionsView> response;
  final keywords = <String>[];

  @override
  Future<CommerceOptionsView> lookup(String keyword) {
    keywords.add(keyword);
    return response;
  }
}

class _FailingCommerceFixture implements CommerceOptionsClient {
  @override
  Future<CommerceOptionsView> lookup(String keyword) async =>
      throw StateError('network failure');
}

Widget _panel({
  required CommerceOptionsClient commerceClient,
  Future<bool> Function(Uri url)? onOpenProductUrl,
}) => MaterialApp(
  home: Scaffold(
    body: Align(
      alignment: Alignment.topRight,
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 320, maxHeight: 450),
        child: ShoppingOverlay(
          client: const _ShoppingFixture('洗衣精'),
          commerceClient: commerceClient,
          onOpenProductUrl: onOpenProductUrl,
          onClose: () {},
        ),
      ),
    ),
  ),
);

Future<void> _openProcurement(WidgetTester tester) async {
  await tester.pumpAndSettle();
  await tester.tap(find.byKey(const Key('shopping-procure-洗衣精')));
  await tester.pumpAndSettle();
}

void main() {
  const success = CommerceOptionsView(
    status: CommerceOptionsStatus.success,
    message: '查詢成功',
    options: [
      CommerceOptionView(
        title: '濃縮洗衣精補充包',
        store: 'momo購物網',
        estimatedPrice: 299,
        productUrl: 'https://shop.example.test/laundry',
        unitSpec: '1.8L × 2包',
      ),
    ],
  );

  testWidgets(
    'procure action queries by the Shopping item name and renders options',
    (tester) async {
      final commerce = _CommerceFixture(Future.value(success));
      await tester.pumpWidget(_panel(commerceClient: commerce));
      await tester.pumpAndSettle();

      expect(find.text('採購 >'), findsOneWidget);
      await _openProcurement(tester);

      expect(commerce.keywords, ['洗衣精']);
      expect(find.text('推薦採購'), findsOneWidget);
      expect(find.text('濃縮洗衣精補充包'), findsOneWidget);
      expect(find.text('momo購物網'), findsOneWidget);
      expect(find.text('NT\$299'), findsOneWidget);
      expect(find.text('1.8L × 2包'), findsOneWidget);
    },
  );

  testWidgets('purchase link uses only the returned product URL', (
    tester,
  ) async {
    final commerce = _CommerceFixture(Future.value(success));
    Uri? opened;
    await tester.pumpWidget(
      _panel(
        commerceClient: commerce,
        onOpenProductUrl: (url) async {
          opened = url;
          return true;
        },
      ),
    );

    await _openProcurement(tester);
    await tester.tap(find.byKey(const Key('commerce-open-濃縮洗衣精補充包')));
    await tester.pump();

    expect(opened, Uri.parse('https://shop.example.test/laundry'));
  });

  testWidgets('no match result is honest and recoverable', (tester) async {
    final noMatch = _CommerceFixture(
      Future.value(
        const CommerceOptionsView(
          status: CommerceOptionsStatus.noMatch,
          message: '找不到',
        ),
      ),
    );
    await tester.pumpWidget(_panel(commerceClient: noMatch));
    await _openProcurement(tester);
    expect(find.text('找不到「洗衣精」的採購推薦。'), findsOneWidget);
  });

  testWidgets('an empty success response does not invent recommendations', (
    tester,
  ) async {
    await tester.pumpWidget(
      _panel(
        commerceClient: _CommerceFixture(
          Future.value(
            const CommerceOptionsView(
              status: CommerceOptionsStatus.success,
              message: '查詢成功',
            ),
          ),
        ),
      ),
    );

    await _openProcurement(tester);

    expect(find.text('找不到「洗衣精」的採購推薦。'), findsOneWidget);
    expect(find.text('NT\$'), findsNothing);
  });

  testWidgets('network failures show a recoverable message', (tester) async {
    await tester.pumpWidget(_panel(commerceClient: _FailingCommerceFixture()));
    await _openProcurement(tester);
    expect(find.text('目前無法取得採購推薦，請稍後再試。'), findsOneWidget);
  });

  testWidgets('malformed product URLs do not offer an unsafe external action', (
    tester,
  ) async {
    final commerce = _CommerceFixture(
      Future.value(
        const CommerceOptionsView(
          status: CommerceOptionsStatus.success,
          message: '查詢成功',
          options: [
            CommerceOptionView(
              title: '沒有可用連結的洗衣精',
              productUrl: 'javascript:alert(1)',
            ),
          ],
        ),
      ),
    );
    await tester.pumpWidget(_panel(commerceClient: commerce));
    await _openProcurement(tester);

    expect(find.text('沒有可用連結的洗衣精'), findsOneWidget);
    expect(find.text('前往購買 >'), findsNothing);
    expect(tester.takeException(), isNull);
  });

  testWidgets('three recommendations scroll inside a bounded sheet', (
    tester,
  ) async {
    await tester.binding.setSurfaceSize(const Size(360, 568));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    const options = [
      CommerceOptionView(
        title: '第一個長篇推薦商品名稱，確保卡片需要足夠的垂直空間顯示。',
        store: '商店一',
        estimatedPrice: 199,
        productUrl: 'https://shop.example.test/one',
        unitSpec: '大容量補充組',
      ),
      CommerceOptionView(
        title: '第二個長篇推薦商品名稱，確保卡片需要足夠的垂直空間顯示。',
        store: '商店二',
        estimatedPrice: 299,
        productUrl: 'https://shop.example.test/two',
        unitSpec: '家庭號組合',
      ),
      CommerceOptionView(
        title: '第三個長篇推薦商品名稱，確保卡片需要足夠的垂直空間顯示。',
        store: '商店三',
        estimatedPrice: 399,
        productUrl: 'https://shop.example.test/three',
        unitSpec: '超值囤貨包',
      ),
    ];
    await tester.pumpWidget(
      _panel(
        commerceClient: _CommerceFixture(
          Future.value(
            const CommerceOptionsView(
              status: CommerceOptionsStatus.success,
              message: '查詢成功',
              options: options,
            ),
          ),
        ),
      ),
    );

    await _openProcurement(tester);
    expect(find.text('推薦採購'), findsOneWidget);
    expect(find.text(options.first.title), findsOneWidget);

    await tester.drag(
      find.byKey(const Key('commerce-options-list')),
      const Offset(0, -400),
    );
    await tester.pumpAndSettle();

    expect(find.text(options.last.title), findsOneWidget);
    expect(
      find.byKey(const Key('commerce-open-第三個長篇推薦商品名稱，確保卡片需要足夠的垂直空間顯示。')),
      findsOneWidget,
    );
    expect(find.text('推薦採購'), findsOneWidget);
    expect(tester.takeException(), isNull);
  });
}
