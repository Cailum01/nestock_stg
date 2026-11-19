import 'package:nestock/features/onboarding/domain/household_context.dart';

const testHouseholdContext = TestHouseholdRequestContext();

class TestHouseholdRequestContext implements HouseholdRequestContext {
  const TestHouseholdRequestContext({
    this.bearerCredential = 'test-install-token',
    this.householdId = 'test-household',
  });

  final String bearerCredential;
  final String householdId;

  @override
  HouseholdRequestScope snapshot({String? householdIdOverride}) =>
      HouseholdRequestScope(
        bearerCredential: bearerCredential,
        householdId: householdIdOverride ?? householdId,
      );
}
