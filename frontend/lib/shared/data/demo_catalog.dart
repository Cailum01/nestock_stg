import 'catalog_client.dart';

class DemoCatalog implements CatalogClient {
  const DemoCatalog();
  @override
  Future<List<ItemSummary>> inventory() async => const [
    ItemSummary(name: '白米', detail: '2 包 · 廚房櫃子 · 已確認數量（示範）'),
  ];
  @override
  Future<List<ItemSummary>> shopping() async => const [
    ItemSummary(name: '牛奶', detail: '1 瓶 · 待購買（示範）'),
  ];
}
