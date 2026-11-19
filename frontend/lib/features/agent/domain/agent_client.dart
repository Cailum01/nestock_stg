import 'agent_view_data.dart';

/// One structured change to an operation on a pending approval card.
///
/// `resetPurchasedOn` deliberately distinguishes an omitted purchase day from
/// an explicit JSON null, which tells the backend to use today's default.
/// `clearLocation` likewise sends an explicit `location: null` rather than
/// omitting the field on eligible new-item cards.
class CardChange {
  factory CardChange({
    required String operationId,
    String? name,
    int? quantity,
    String? location,
    bool clearLocation = false,
    String? purchasedOn,
    bool resetPurchasedOn = false,
  }) {
    if (operationId.trim().isEmpty) {
      throw ArgumentError.value(
        operationId,
        'operationId',
        'must not be blank',
      );
    }
    if (quantity != null && quantity < 1) {
      throw ArgumentError.value(quantity, 'quantity', 'must be at least one');
    }
    final normalizedName = name?.trim();
    if (normalizedName != null && normalizedName.isEmpty) {
      throw ArgumentError.value(name, 'name', 'must not be blank');
    }
    final normalizedLocation = location?.trim();
    if (normalizedLocation != null && normalizedLocation.isEmpty) {
      throw ArgumentError.value(location, 'location', 'must not be blank');
    }
    if (normalizedLocation != null && clearLocation) {
      throw ArgumentError('A location cannot be set and cleared together.');
    }
    if (purchasedOn != null && !_isCalendarDay(purchasedOn)) {
      throw ArgumentError.value(
        purchasedOn,
        'purchasedOn',
        'must use YYYY-MM-DD',
      );
    }
    if (purchasedOn != null && resetPurchasedOn) {
      throw ArgumentError('A purchase date cannot be set and reset together.');
    }
    if (normalizedName == null &&
        quantity == null &&
        normalizedLocation == null &&
        !clearLocation &&
        purchasedOn == null &&
        !resetPurchasedOn) {
      throw ArgumentError('A card change needs at least one changed field.');
    }
    return CardChange._(
      operationId: operationId.trim(),
      name: normalizedName,
      quantity: quantity,
      location: normalizedLocation,
      includesLocation: normalizedLocation != null || clearLocation,
      purchasedOn: purchasedOn,
      includesPurchasedOn: purchasedOn != null || resetPurchasedOn,
    );
  }

  const CardChange._({
    required this.operationId,
    required this.name,
    required this.quantity,
    required this.location,
    required this.includesLocation,
    required this.purchasedOn,
    required this.includesPurchasedOn,
  });

  final String operationId;
  final String? name;
  final int? quantity;
  final String? location;
  final bool includesLocation;
  final String? purchasedOn;
  final bool includesPurchasedOn;

  Map<String, dynamic> toJson() => {
    'operation_id': operationId,
    if (name != null) 'name': name,
    if (quantity != null) 'quantity': quantity,
    if (includesLocation) 'location': location,
    if (includesPurchasedOn) 'purchased_on': purchasedOn,
  };
}

final _purchaseDayPattern = RegExp(r'^\d{4}-\d{2}-\d{2}$');

bool _isCalendarDay(String value) {
  if (!_purchaseDayPattern.hasMatch(value)) return false;
  final pieces = value.split('-').map(int.parse).toList(growable: false);
  final day = DateTime(pieces[0], pieces[1], pieces[2]);
  return day.year == pieces[0] &&
      day.month == pieces[1] &&
      day.day == pieces[2];
}

/// Provisional household-scoped application port. No LangGraph or HTTP details.
/// A1/C2 will agree the wire adapter against backend Pydantic/OpenAPI models.
abstract interface class AgentClient {
  Future<AgentReply> send(String message, {String? replacesApprovalId});

  /// Submit card changes without asking the Agent model to reinterpret them.
  Future<AgentReply> submitCardChanges({
    required String approvalId,
    required List<CardChange> changes,
  });

  /// Retry this same approval identity after a timeout; never mint a new write.
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  });
}

/// Optional port for carrying the household scope already bound to a card.
abstract interface class HouseholdBoundAgentClient {
  Future<AgentReply> submitCardChangesInHousehold({
    required String approvalId,
    required List<CardChange> changes,
    required String householdId,
  });

  Future<AgentReply> resumeInHousehold({
    required String approvalId,
    required bool approved,
    required String householdId,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  });
}

/// Optional capability for sending an uploaded, opaque photo reference.
abstract interface class PhotoAwareAgentClient implements AgentClient {
  Future<AgentReply> sendWithPhoto(String message, String imageRef);
}
