import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:image/image.dart' as image;
import 'package:nestock/features/photo/photo_upload.dart';

void main() {
  test('authorizes and PUTs JPEG bytes with supplied headers', () async {
    var calls = 0;
    final client = MockClient((request) async {
      calls++;
      if (calls == 1) {
        expect(request.method, 'POST');
        expect(
          request.url.toString(),
          'http://10.0.2.2:8000/api/media/upload-authorization',
        );
        final body = jsonDecode(request.body) as Map<String, dynamic>;
        expect(body['household_id'], 'demo-household');
        expect(body['filename'], 'camera_capture.jpg');
        expect(body['content_type'], 'image/jpeg');
        expect(body['file_size_bytes'], greaterThan(0));
        return http.Response(
          jsonEncode({
            'upload_url': 'http://127.0.0.1:8000/staging/upload/token',
            'image_ref': 'opaque:unaltered',
            'expires_in_seconds': 300,
            'required_headers': {
              'Content-Type': 'image/jpeg',
              'x-ms-blob-type': 'BlockBlob',
            },
          }),
          200,
        );
      }
      expect(request.method, 'PUT');
      expect(request.url.host, '10.0.2.2');
      expect(request.headers['x-ms-blob-type'], 'BlockBlob');
      final decoded = image.decodeJpg(request.bodyBytes)!;
      expect(decoded.width, 1920);
      expect(decoded.height, 960);
      return http.Response('', 201);
    });
    final uploader = HttpPhotoUpload(
      config: PhotoNetworkConfig(
        apiBase: Uri.parse('http://10.0.2.2:8000'),
        emulatorHost: '10.0.2.2',
      ),
      client: client,
    );
    final input = image.encodePng(image.Image(width: 2000, height: 1000));
    expect(await uploader.upload(input), 'opaque:unaltered');
    expect(calls, 2);
  });
  test('remote upload urls stay unchanged', () {
    final config = PhotoNetworkConfig(
      apiBase: Uri.parse('http://10.0.2.2:8000'),
      emulatorHost: '10.0.2.2',
    );
    final remote = Uri.parse('https://example.blob.core.windows.net/a?sig=abc');
    expect(config.adaptUploadUrl(remote), remote);
  });
}
