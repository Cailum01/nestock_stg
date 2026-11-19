import 'package:flutter_secure_storage/flutter_secure_storage.dart';

abstract interface class SecureCredentialStore {
  Future<String?> readBearerCredential();
  Future<void> writeBearerCredential(String credential);
  Future<void> deleteBearerCredential();
}

/// Stores the install bearer only in Keychain/Keystore-backed storage.
class FlutterSecureCredentialStore implements SecureCredentialStore {
  FlutterSecureCredentialStore({FlutterSecureStorage? storage})
    : _storage = storage ?? const FlutterSecureStorage();

  static const _credentialKey = 'nestock.install.bearer';
  final FlutterSecureStorage _storage;

  @override
  Future<String?> readBearerCredential() => _storage.read(key: _credentialKey);

  @override
  Future<void> writeBearerCredential(String credential) =>
      _storage.write(key: _credentialKey, value: credential);

  @override
  Future<void> deleteBearerCredential() => _storage.delete(key: _credentialKey);
}
