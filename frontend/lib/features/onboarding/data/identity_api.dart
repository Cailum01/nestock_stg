import 'dart:convert';

import 'package:http/http.dart' as http;

import '../domain/identity_models.dart';
import 'secure_transport_client.dart';

class IssuedOneTimeCode {
  const IssuedOneTimeCode({required this.value, required this.expiresAt});

  final String value;
  final DateTime? expiresAt;
}

class GoogleIdentitySession {
  const GoogleIdentitySession({
    required this.bearerCredential,
    required this.householdId,
  });

  final String bearerCredential;
  final String householdId;
}

class IdentityApi {
  IdentityApi({required this.baseUrl, http.Client? client})
    : _client = SecureTransportClient(innerClient: client);

  final Uri baseUrl;
  final http.Client _client;

  Future<GoogleIdentitySession> authenticateGoogle(String idToken) async {
    final body = await _send(
      'POST',
      '/identity/google',
      jsonBody: {'google_id_token': idToken},
    );
    return GoogleIdentitySession(
      bearerCredential: _requiredString(body, 'bearer_credential'),
      householdId: _requiredString(body, 'household_id'),
    );
  }

  Future<String> bootstrap(String challenge) async {
    final body = await _send(
      'POST',
      '/identity/bootstrap',
      jsonBody: {'bootstrap_challenge': challenge},
    );
    return _requiredString(body, 'bearer_credential');
  }

  Future<List<HouseholdMembershipView>> listHouseholds(String bearer) async {
    final body = await _send('GET', '/households', bearer: bearer);
    final raw = body['households'];
    if (raw is! List) {
      throw const FormatException('Household list is malformed.');
    }
    return raw
        .map((entry) {
          if (entry is! Map) {
            throw const FormatException('Household entry is malformed.');
          }
          final record = Map<String, dynamic>.from(entry);
          return HouseholdMembershipView(
            householdId: _requiredString(record, 'household_id'),
            role: _roleFromJson(record['role']),
          );
        })
        .toList(growable: false);
  }

  Future<String> createHousehold(String bearer) async {
    final body = await _send(
      'POST',
      '/households',
      bearer: bearer,
      jsonBody: const {},
    );
    return _requiredString(body, 'household_id');
  }

  Future<String> redeemInvite(String bearer, String inviteCode) async {
    final body = await _send(
      'POST',
      '/households/invites/redeem',
      bearer: bearer,
      jsonBody: {'invite_code': inviteCode},
    );
    return _requiredString(body, 'household_id');
  }

  Future<IssuedOneTimeCode> createInvite({
    required String bearer,
    required String householdId,
  }) async {
    final body = await _send(
      'POST',
      '/households/invites',
      bearer: bearer,
      householdId: householdId,
      jsonBody: const {},
    );
    return IssuedOneTimeCode(
      value: _requiredString(body, 'invite_code'),
      expiresAt: _optionalDateTime(body['expires_at']),
    );
  }

  Future<void> leaveHousehold({
    required String bearer,
    required String householdId,
  }) async {
    await _send(
      'POST',
      '/households/leave',
      bearer: bearer,
      householdId: householdId,
      jsonBody: const {},
    );
  }

  Future<void> revokeMembership({
    required String bearer,
    required String householdId,
    required String deviceId,
  }) async {
    await _send(
      'POST',
      '/households/memberships/revoke',
      bearer: bearer,
      householdId: householdId,
      jsonBody: {'device_id': deviceId},
    );
  }

  Future<IssuedOneTimeCode> issueDeviceRecoveryCode(String bearer) async {
    final body = await _send(
      'POST',
      '/identity/device-recovery-codes',
      bearer: bearer,
      jsonBody: const {},
    );
    return IssuedOneTimeCode(
      value: _requiredString(body, 'recovery_code'),
      expiresAt: null,
    );
  }

  Future<void> redeemDeviceRecoveryCode(String code) async {
    await _send(
      'POST',
      '/identity/device-recovery/redeem',
      jsonBody: {'recovery_code': code},
    );
  }

  Future<IssuedOneTimeCode> issueHouseholdRecoveryCode({
    required String bearer,
    required String householdId,
  }) async {
    final body = await _send(
      'POST',
      '/households/recovery-codes',
      bearer: bearer,
      householdId: householdId,
      jsonBody: const {},
    );
    return IssuedOneTimeCode(
      value: _requiredString(body, 'recovery_code'),
      expiresAt: null,
    );
  }

  Future<String> redeemHouseholdRecoveryCode({
    required String bearer,
    required String code,
  }) async {
    final body = await _send(
      'POST',
      '/households/recovery/redeem',
      bearer: bearer,
      jsonBody: {'recovery_code': code},
    );
    return _requiredString(body, 'household_id');
  }

  Future<Map<String, dynamic>> _send(
    String method,
    String path, {
    String? bearer,
    String? householdId,
    Map<String, dynamic>? jsonBody,
  }) async {
    final headers = <String, String>{};
    if (jsonBody != null) headers['Content-Type'] = 'application/json';
    if (bearer != null) headers['Authorization'] = 'Bearer $bearer';
    if (householdId != null) headers['X-Household-Id'] = householdId;
    final request = http.Request(method, baseUrl.resolve(path))
      ..headers.addAll(headers);
    if (jsonBody != null) request.body = jsonEncode(jsonBody);
    final streamed = await _client
        .send(request)
        .timeout(const Duration(seconds: 20));
    final response = await http.Response.fromStream(streamed);
    if (response.statusCode < 200 || response.statusCode >= 300) {
      throw _failure(response, path);
    }
    if (response.body.isEmpty) return const {};
    final decoded = jsonDecode(response.body);
    if (decoded is! Map) {
      throw const FormatException('Identity response must be an object.');
    }
    return Map<String, dynamic>.from(decoded);
  }

  IdentityHttpFailure _failure(http.Response response, String path) {
    final throttled = response.statusCode == 429;
    final oneTimeCodeRejected =
        response.statusCode == 400 ||
        response.statusCode == 410 ||
        response.statusCode == 422 ||
        response.statusCode == 401 && path == '/identity/bootstrap';
    final message = throttled
        ? 'Too many attempts. Wait and try again.'
        : oneTimeCodeRejected
        ? 'This one-time code is invalid, expired or already used.'
        : response.statusCode == 401
        ? 'This install credential is no longer valid. Recover the install.'
        : response.statusCode == 409
        ? 'This household membership action is no longer allowed.'
        : 'Identity request failed (${response.statusCode}).';
    return IdentityHttpFailure(
      statusCode: response.statusCode,
      message: message,
    );
  }

  String _requiredString(Map<String, dynamic> body, String key) {
    final value = body[key];
    if (value is! String || value.trim().isEmpty) {
      throw FormatException('Identity response field "$key" is missing.');
    }
    return value;
  }

  HouseholdRole _roleFromJson(dynamic value) => switch (value) {
    'owner' => HouseholdRole.owner,
    'admin' => HouseholdRole.admin,
    'member' => HouseholdRole.member,
    _ => throw const FormatException('Unknown household role.'),
  };

  DateTime? _optionalDateTime(dynamic value) =>
      value is String ? DateTime.tryParse(value) : null;
}
