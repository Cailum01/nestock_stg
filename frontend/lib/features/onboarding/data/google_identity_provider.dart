import 'package:google_sign_in/google_sign_in.dart';

abstract interface class GoogleIdentityProvider {
  bool get isConfigured;
  Future<String?> attemptSilentSignIn();
  Future<String> signIn();
}

class GoogleSignInIdentityProvider implements GoogleIdentityProvider {
  GoogleSignInIdentityProvider({required this.serverClientId})
    : _googleSignIn = GoogleSignIn.instance;

  final String serverClientId;
  final GoogleSignIn _googleSignIn;
  Future<void>? _initialization;

  @override
  bool get isConfigured => serverClientId.trim().isNotEmpty;

  Future<void> _initialize() {
    if (!isConfigured) {
      throw StateError('Google sign-in is not configured.');
    }
    return _initialization ??= _googleSignIn.initialize(
      serverClientId: serverClientId,
    );
  }

  @override
  Future<String?> attemptSilentSignIn() async {
    await _initialize();
    final attempt = _googleSignIn.attemptLightweightAuthentication();
    if (attempt == null) return null;
    final account = await attempt;
    return account == null ? null : _requiredIdToken(account);
  }

  @override
  Future<String> signIn() async {
    await _initialize();
    return _requiredIdToken(await _googleSignIn.authenticate());
  }

  String _requiredIdToken(GoogleSignInAccount account) {
    final token = account.authentication.idToken;
    if (token == null || token.isEmpty) {
      throw StateError('Google did not return an ID token.');
    }
    return token;
  }
}
