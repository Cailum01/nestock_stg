/// Minimal display projection, not a duplicate domain/wire schema.
class ItemSummary {
  const ItemSummary({required this.name, required this.detail});
  final String name;
  final String detail;
}

/// Provisional read-only boundary. C2 owns final API/view mapping.
abstract interface class CatalogClient {
  Future<List<ItemSummary>> inventory();
  Future<List<ItemSummary>> shopping();
}
