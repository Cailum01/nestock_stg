import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

/// Validates request destinations before forwarding requests with credentials.
class SecureTransportClient extends http.BaseClient {
  SecureTransportClient({
    http.Client? innerClient,
    this.allowInsecureLoopback = kDebugMode,
  }) : _innerClient = innerClient ?? http.Client();

  final http.Client _innerClient;
  final bool allowInsecureLoopback;

  @override
  Future<http.StreamedResponse> send(http.BaseRequest request) {
    SecureUrlPolicy.validate(
      request.url,
      allowInsecureLoopback: allowInsecureLoopback,
    );
    // Never let an HTTP implementation forward credentials across redirects.
    request.followRedirects = false;
    return _innerClient.send(request);
  }

  @override
  void close() => _innerClient.close();
}

class SecureUrlPolicy {
  const SecureUrlPolicy._();

  static bool isAllowed(Uri uri, {required bool allowInsecureLoopback}) {
    if (!uri.hasScheme ||
        !uri.hasAuthority ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        (uri.scheme != 'https' && uri.scheme != 'http')) {
      return false;
    }
    return uri.scheme == 'https' ||
        allowInsecureLoopback && _isLoopbackDevelopmentHost(uri.host);
  }

  static Uri validate(Uri uri, {required bool allowInsecureLoopback}) {
    if (!isAllowed(uri, allowInsecureLoopback: allowInsecureLoopback)) {
      throw ArgumentError('Invalid or insecure network URL.');
    }
    return uri;
  }

  static bool _isLoopbackDevelopmentHost(String host) {
    final normalizedHost = host.toLowerCase();
    if (normalizedHost == 'localhost' ||
        normalizedHost == '::1' ||
        // Android emulator's documented host-side development bridge.
        normalizedHost == '10.0.2.2') {
      return true;
    }
    final octets = normalizedHost.split('.');
    if (octets.length != 4) return false;
    final values = octets.map(int.tryParse).toList(growable: false);
    return values.every(
          (value) => value != null && value >= 0 && value <= 255,
        ) &&
        values[0] == 127;
  }
}
