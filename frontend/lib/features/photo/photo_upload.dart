import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:image/image.dart' as image;

import '../onboarding/domain/household_context.dart';
import '../onboarding/data/secure_transport_client.dart';

abstract interface class PhotoUpload {
  Future<String> upload(Uint8List photo);
}

/// Configuration is explicit; only the provisional local origin is adapted.
class PhotoNetworkConfig {
  const PhotoNetworkConfig({required this.apiBase, this.emulatorHost});
  factory PhotoNetworkConfig.forApiBase(Uri apiBase) => PhotoNetworkConfig(
    apiBase: apiBase,
    emulatorHost: apiBase.host == '10.0.2.2' ? '10.0.2.2' : null,
  );
  final Uri apiBase;
  final String? emulatorHost;

  Uri adaptUploadUrl(Uri uri) {
    if (emulatorHost != null &&
        uri.scheme == 'http' &&
        (uri.host == '127.0.0.1' || uri.host == 'localhost') &&
        uri.port == 8000) {
      return uri.replace(host: emulatorHost);
    }
    return uri;
  }
}

class HttpPhotoUpload implements PhotoUpload {
  HttpPhotoUpload({
    required this.config,
    required this.householdContext,
    http.Client? client,
    this.timeout = const Duration(seconds: 20),
  }) : client = SecureTransportClient(innerClient: client);
  final PhotoNetworkConfig config;
  final HouseholdRequestContext householdContext;
  final http.Client client;
  final Duration timeout;

  @override
  Future<String> upload(Uint8List photo) async {
    final decoded = image.decodeImage(photo);
    if (decoded == null) throw const FormatException('Invalid image');
    var oriented = image.bakeOrientation(decoded);
    if (oriented.width > 1920 || oriented.height > 1920) {
      oriented = oriented.width >= oriented.height
          ? image.copyResize(oriented, width: 1920)
          : image.copyResize(oriented, height: 1920);
    }
    final bytes = Uint8List.fromList(image.encodeJpg(oriented, quality: 85));
    if (bytes.length > 10485760) {
      throw const FormatException('Image exceeds 10 MiB');
    }
    final scope = householdContext.snapshot();
    final auth = await client
        .post(
          config.apiBase.resolve('/api/media/upload-authorization'),
          headers: {...scope.headers, 'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': scope.householdId,
            'filename': 'camera_capture.jpg',
            'content_type': 'image/jpeg',
            'file_size_bytes': bytes.length,
          }),
        )
        .timeout(timeout);
    if (auth.statusCode != 200) throw StateError('Authorization failed');
    final data = jsonDecode(auth.body) as Map<String, dynamic>;
    final ref = data['image_ref'] as String;
    if (ref.isEmpty) throw const FormatException('Empty image reference');
    final headers = Map<String, String>.from(data['required_headers'] as Map);
    final uploadHeaders = Map<String, String>.fromEntries(
      headers.entries.where(
        (entry) =>
            entry.key.toLowerCase() != 'authorization' &&
            entry.key.toLowerCase() != 'x-household-id',
      ),
    );
    final uploadUri = config.adaptUploadUrl(
      Uri.parse(data['upload_url'] as String),
    );
    final putHeaders = <String, String>{...uploadHeaders};
    if (_hasSameOrigin(uploadUri, config.apiBase)) {
      putHeaders.addAll(scope.headers);
    }
    final result = await client
        .put(uploadUri, headers: putHeaders, body: bytes)
        .timeout(timeout);
    if (result.statusCode != 200 && result.statusCode != 201) {
      throw StateError('Upload failed');
    }
    return ref;
  }
}

bool _hasSameOrigin(Uri left, Uri right) {
  int effectivePort(Uri uri) {
    if (uri.hasPort) return uri.port;
    return switch (uri.scheme.toLowerCase()) {
      'http' => 80,
      'https' => 443,
      _ => -1,
    };
  }

  return left.scheme.toLowerCase() == right.scheme.toLowerCase() &&
      left.host.toLowerCase() == right.host.toLowerCase() &&
      effectivePort(left) == effectivePort(right);
}
