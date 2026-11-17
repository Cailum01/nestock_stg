import 'agent_view_data.dart';

/// One structured change to an operation on a pending approval card.
///
/// `resetPurchasedOn` deliberately distinguishes an omitted purchase day from
/// an explicit JSON null, which tells the backend to use today's default.
class CardChange {
  factory CardChange({
    required String operationId,
    int? quantity,
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
    if (quantity == null && purchasedOn == null && !resetPurchasedOn) {
      throw ArgumentError('A card change needs at least one changed field.');
    }
    return CardChange._(
      operationId: operationId.trim(),
      quantity: quantity,
      purchasedOn: purchasedOn,
      includesPurchasedOn: purchasedOn != null || resetPurchasedOn,
    );
  }

  const CardChange._({
    required this.operationId,
    required this.quantity,
    required this.purchasedOn,
    required this.includesPurchasedOn,
  });

  final String operationId;
  final int? quantity;
  final String? purchasedOn;
  final bool includesPurchasedOn;

  Map<String, dynamic> toJson() => {
    'operation_id': operationId,
    if (quantity != null) 'quantity': quantity,
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

/// Optional capability for sending an uploaded, opaque photo reference.
abstract interface class PhotoAwareAgentClient implements AgentClient {
  Future<AgentReply> sendWithPhoto(String message, String imageRef);
}
