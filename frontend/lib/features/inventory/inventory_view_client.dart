/// Presentation projections only; these do not define the backend wire contract.
class InventoryViewItem {
  const InventoryViewItem(
    this.name,
    this.quantity,
    this.unit, {
    this.aliases = const [],
    this.location,
    this.expiry,
  });
  final String name, quantity, unit;
  final List<String> aliases;
  final String? location;
  final DateTime? expiry;
}

abstract interface class InventoryViewClient {
  Future<List<InventoryViewItem>> load();
}

class DemoInventoryViewClient implements InventoryViewClient {
  const DemoInventoryViewClient();
  @override
  Future<List<InventoryViewItem>> load() async => [
    InventoryViewItem('白米', '2', '包', location: '廚房櫃子'),
    InventoryViewItem(
      '咖啡',
      '2',
      '包',
      location: '廚房',
      expiry: DateTime(2026, 11, 13),
    ),
    InventoryViewItem(
      '衛生紙',
      '3',
      '包',
      location: '儲藏櫃',
      aliases: ['廁所紙', 'toilet paper'],
    ),
    InventoryViewItem('牛奶', '1', '瓶', expiry: DateTime(2026, 12, 1)),
    const InventoryViewItem('肥皂', '2', '個'),
  ];
}
