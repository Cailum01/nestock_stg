enum HouseholdRole { owner, admin, member }

class HouseholdMembershipView {
  const HouseholdMembershipView({
    required this.householdId,
    required this.role,
  });

  final String householdId;
  final HouseholdRole role;

  bool get canManageMembers =>
      role == HouseholdRole.owner || role == HouseholdRole.admin;
}

class IdentityHttpFailure implements Exception {
  const IdentityHttpFailure({required this.statusCode, required this.message});

  final int statusCode;
  final String message;

  @override
  String toString() => message;
}
