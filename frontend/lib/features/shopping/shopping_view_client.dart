class ShoppingViewItem {
  const ShoppingViewItem(this.name, this.quantity, this.unit, this.status);
  final String name, quantity, unit, status;
}

abstract interface class ShoppingViewClient {
  Future<List<ShoppingViewItem>> load();
}

class DemoShoppingViewClient implements ShoppingViewClient {
  const DemoShoppingViewClient();
  @override
  Future<List<ShoppingViewItem>> load() async => const [
    ShoppingViewItem('牛奶', '1', '瓶', 'pending'),
    ShoppingViewItem('衛生紙', '1', '包', 'pending'),
    ShoppingViewItem('咖啡豆', '2', '包', 'pending'),
  ];
}
