/// An immutable snapshot of the install credential and selected household.
///
/// Capturing both together prevents a household switch from changing the
/// authorization scope of a request that is already in flight.
class HouseholdRequestScope {
  const HouseholdRequestScope({
    required this.bearerCredential,
    required this.householdId,
  });

  final String bearerCredential;
  final String householdId;

  Map<String, String> get headers => {
    'Authorization': 'Bearer $bearerCredential',
    'X-Household-Id': householdId,
  };
}

abstract interface class HouseholdRequestContext {
  HouseholdRequestScope snapshot({String? householdIdOverride});
}
