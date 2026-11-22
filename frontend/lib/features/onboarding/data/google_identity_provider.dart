import 'package:google_sign_in/google_sign_in.dart';

abstract interface class GoogleIdentityProvider {
  bool get isConfigured;
  Future<String> signIn();
}

class GoogleSignInCancelled implements Exception {
  const GoogleSignInCancelled();
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
  Future<String> signIn() async {
    await _initialize();
    try {
      return _requiredIdToken(await _googleSignIn.authenticate());
    } on GoogleSignInException catch (error) {
      if (error.code == GoogleSignInExceptionCode.canceled) {
        throw const GoogleSignInCancelled();
      }
      rethrow;
    }
  }

  String _requiredIdToken(GoogleSignInAccount account) {
    final token = account.authentication.idToken;
    if (token == null || token.isEmpty) {
      throw StateError('Google did not return an ID token.');
    }
    return token;
  }
}
