/// Compile-time presentation switches for optional approval-card UI.
///
/// This does not enable a backend feature. It only exposes editing controls
/// once the matching backend contract has been enabled and deployed.
class ApprovalCardConfig {
  const ApprovalCardConfig({
    this.datedPurchasesEnabled = defaultDatedPurchasesEnabled,
  });

  static const defaultDatedPurchasesEnabled = bool.fromEnvironment(
    'NESTOCK_DATED_PURCHASES',
  );

  final bool datedPurchasesEnabled;
}
