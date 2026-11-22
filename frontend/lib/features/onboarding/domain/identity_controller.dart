import 'package:flutter/foundation.dart';

import '../data/identity_api.dart';
import '../data/google_identity_provider.dart';
import '../data/secure_credential_store.dart';
import '../data/secure_transport_client.dart';
import 'household_context.dart';
import 'identity_models.dart';

/// In-memory session coordinator. Only the bearer is persisted, via the
/// injected OS secure-storage implementation.
class IdentityController extends ChangeNotifier
    implements HouseholdRequestContext {
  IdentityController({
    required this.api,
    required this.credentialStore,
    this.googleIdentityProvider,
  });

  final IdentityApi api;
  final SecureCredentialStore credentialStore;
  final GoogleIdentityProvider? googleIdentityProvider;
  String? _bearer;
  List<HouseholdMembershipView> _households = const [];
  String? _selectedHouseholdId;
  bool _loading = false;
  String? _error;
  bool _initialized = false;

  bool get initialized => _initialized;
  bool get loading => _loading;
  String? get error => _error;
  bool get apiHostConfigured =>
      SecureUrlPolicy.isAllowed(api.baseUrl, allowInsecureLoopback: kDebugMode);
  bool get hasCredential => _bearer != null;
  bool get canSignInWithGoogle => googleIdentityProvider?.isConfigured ?? false;
  List<HouseholdMembershipView> get households => _households;
  String? get selectedHouseholdId => _selectedHouseholdId;
  HouseholdMembershipView? get selectedHousehold {
    for (final membership in _households) {
      if (membership.householdId == _selectedHouseholdId) return membership;
    }
    return null;
  }

  @override
  HouseholdRequestScope snapshot({String? householdIdOverride}) {
    final bearer = _bearer;
    final householdId = householdIdOverride ?? _selectedHouseholdId;
    if (bearer == null || bearer.isEmpty) {
      throw StateError('This install must be authenticated first.');
    }
    if (householdId == null ||
        !_households.any((household) => household.householdId == householdId)) {
      throw StateError('Select an active household before continuing.');
    }
    return HouseholdRequestScope(
      bearerCredential: bearer,
      householdId: householdId,
    );
  }

  Future<void> initialize({bool launchGoogleSignIn = true}) async {
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      _bearer = await credentialStore.readBearerCredential();
      if (_bearer != null) {
        await _refreshHouseholds();
      } else if (launchGoogleSignIn && canSignInWithGoogle) {
        await _authenticateWithGoogle();
      }
    } catch (error) {
      _reportGoogleFailure(error);
    } finally {
      _initialized = true;
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> retry() async {
    _error = null;
    _loading = true;
    notifyListeners();
    try {
      _bearer ??= await credentialStore.readBearerCredential();
      if (_bearer != null) {
        await _refreshHouseholds();
      }
    } catch (error) {
      _reportFailure('retry', error);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> createWithChallenge(String challenge) async {
    final bearer = await _bootstrap(challenge);
    final householdId = await api.createHousehold(bearer);
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<void> signInWithGoogle() async {
    if (!canSignInWithGoogle) {
      _error = 'Google sign-in is not configured.';
      notifyListeners();
      return;
    }
    _loading = true;
    _error = null;
    notifyListeners();
    try {
      await _authenticateWithGoogle();
    } catch (error) {
      _reportGoogleFailure(error);
    } finally {
      _loading = false;
      notifyListeners();
    }
  }

  Future<void> joinWithChallenge(String challenge, String inviteCode) async {
    final bearer = await _bootstrap(challenge);
    final householdId = await api.redeemInvite(bearer, inviteCode);
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<void> recoverHouseholdWithChallenge(
    String challenge,
    String recoveryCode,
  ) async {
    final bearer = await _bootstrap(challenge);
    final householdId = await api.redeemHouseholdRecoveryCode(
      bearer: bearer,
      code: recoveryCode,
    );
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<void> recoverDeviceWithChallenge(
    String challenge,
    String recoveryCode,
  ) async {
    await _bootstrap(challenge);
    await api.redeemDeviceRecoveryCode(recoveryCode);
    await _refreshHouseholds();
  }

  Future<void> createHousehold() async {
    final bearer = _requireBearer();
    final householdId = await api.createHousehold(bearer);
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<void> joinHousehold(String inviteCode) async {
    final householdId = await api.redeemInvite(_requireBearer(), inviteCode);
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<IssuedOneTimeCode> createInvite() => api.createInvite(
    bearer: _requireBearer(),
    householdId: _requireSelectedHouseholdId(),
  );

  Future<void> leaveSelectedHousehold() async {
    final householdId = _requireSelectedHouseholdId();
    await api.leaveHousehold(
      bearer: _requireBearer(),
      householdId: householdId,
    );
    await _refreshHouseholds();
  }

  Future<void> revokeMembership(String deviceId) async {
    await api.revokeMembership(
      bearer: _requireBearer(),
      householdId: _requireSelectedHouseholdId(),
      deviceId: deviceId,
    );
  }

  Future<IssuedOneTimeCode> issueDeviceRecoveryCode() =>
      api.issueDeviceRecoveryCode(_requireBearer());

  Future<IssuedOneTimeCode> issueHouseholdRecoveryCode() =>
      api.issueHouseholdRecoveryCode(
        bearer: _requireBearer(),
        householdId: _requireSelectedHouseholdId(),
      );

  Future<void> redeemHouseholdRecoveryCode(String code) async {
    final householdId = await api.redeemHouseholdRecoveryCode(
      bearer: _requireBearer(),
      code: code,
    );
    await _refreshHouseholds(selectHouseholdId: householdId);
  }

  Future<void> redeemDeviceRecoveryCode(String code) async {
    await api.redeemDeviceRecoveryCode(code);
    await _refreshHouseholds();
  }

  Future<void> clearLocalCredential() async {
    await credentialStore.deleteBearerCredential();
    _bearer = null;
    _households = const [];
    _selectedHouseholdId = null;
    _error = null;
    notifyListeners();
  }

  void selectHousehold(String householdId) {
    if (!_households.any((household) => household.householdId == householdId)) {
      throw ArgumentError.value(householdId, 'householdId');
    }
    _selectedHouseholdId = householdId;
    notifyListeners();
  }

  Future<String> _bootstrap(String challenge) async {
    if (challenge.trim().isEmpty) {
      throw const IdentityHttpFailure(
        statusCode: 0,
        message: 'Enter the one-time setup challenge provided by the operator.',
      );
    }
    final bearer = await api.bootstrap(challenge.trim());
    await credentialStore.writeBearerCredential(bearer);
    _bearer = bearer;
    _error = null;
    notifyListeners();
    return bearer;
  }

  Future<void> _exchangeGoogleToken(String token) async {
    final session = await api.authenticateGoogle(token);
    await credentialStore.writeBearerCredential(session.bearerCredential);
    _bearer = session.bearerCredential;
    await _refreshHouseholds(selectHouseholdId: session.householdId);
  }

  Future<void> _authenticateWithGoogle() async {
    final provider = googleIdentityProvider;
    if (provider == null || !provider.isConfigured) {
      throw StateError('Google sign-in is not configured.');
    }
    await _exchangeGoogleToken(await provider.signIn());
  }

  void _reportGoogleFailure(Object error) {
    if (error is GoogleSignInCancelled) {
      _error = 'Google sign-in was cancelled. Tap Retry to try again.';
      notifyListeners();
      return;
    }
    _reportFailure('Google sign-in', error);
  }

  Future<void> _refreshHouseholds({String? selectHouseholdId}) async {
    final bearer = _requireBearer();
    final households = await api.listHouseholds(bearer);
    _households = households;
    if (selectHouseholdId != null) {
      if (!households.any((entry) => entry.householdId == selectHouseholdId)) {
        throw const FormatException(
          'Default household is missing from the authenticated membership list.',
        );
      }
      _selectedHouseholdId = selectHouseholdId;
    } else if (!_households.any(
      (entry) => entry.householdId == _selectedHouseholdId,
    )) {
      _selectedHouseholdId = _households.firstOrNull?.householdId;
    }
    _error = null;
    notifyListeners();
  }

  String _requireBearer() {
    final value = _bearer;
    if (value == null || value.isEmpty) {
      throw StateError('This install must be authenticated first.');
    }
    return value;
  }

  String _requireSelectedHouseholdId() {
    final value = _selectedHouseholdId;
    if (value == null) throw StateError('Select an active household first.');
    return value;
  }

  void _reportFailure(String operation, Object error) {
    if (kDebugMode) {
      debugPrint('Identity $operation failed (${error.runtimeType}): $error');
    }
    _error = _safeMessage(error);
  }

  String _safeMessage(Object error) => error is IdentityHttpFailure
      ? error.message
      : 'Could not load household access. Check the configured API host and retry.';
}
