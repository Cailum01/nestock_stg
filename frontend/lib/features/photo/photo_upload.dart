import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:image/image.dart' as image;

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
    http.Client? client,
    this.timeout = const Duration(seconds: 20),
  }) : client = client ?? http.Client();
  final PhotoNetworkConfig config;
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
    final auth = await client
        .post(
          config.apiBase.resolve('/api/media/upload-authorization'),
          headers: {'Content-Type': 'application/json'},
          body: jsonEncode({
            'household_id': 'demo-household',
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
    final result = await client
        .put(
          config.adaptUploadUrl(Uri.parse(data['upload_url'] as String)),
          headers: headers,
          body: bytes,
        )
        .timeout(timeout);
    if (result.statusCode != 200 && result.statusCode != 201) {
      throw StateError('Upload failed');
    }
    return ref;
  }
}
