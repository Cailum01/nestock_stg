import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:image/image.dart' as image;
import 'package:nestock/features/photo/photo_upload.dart';

import 'household_test_support.dart';

void main() {
  test('authorizes same-origin JPEG PUTs with household scope', () async {
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
        expect(body['household_id'], 'test-household');
        expect(request.headers['authorization'], 'Bearer test-install-token');
        expect(request.headers['x-household-id'], 'test-household');
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
      expect(request.headers['content-type'], 'image/jpeg');
      expect(request.headers['authorization'], 'Bearer test-install-token');
      expect(request.headers['x-household-id'], 'test-household');
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
      householdContext: testHouseholdContext,
      client: client,
    );
    final input = image.encodePng(image.Image(width: 2000, height: 1000));
    expect(await uploader.upload(input), 'opaque:unaltered');
    expect(calls, 2);
  });

  test(
    'external HTTPS upload does not receive app credentials or scope',
    () async {
      final requests = <http.Request>[];
      final client = MockClient((request) async {
        requests.add(request);
        if (request.method == 'POST') {
          expect(request.headers['authorization'], 'Bearer test-install-token');
          expect(request.headers['x-household-id'], 'test-household');
          return http.Response(
            jsonEncode({
              'upload_url':
                  'https://storage.example.net/object?signature=opaque',
              'image_ref': 'opaque:external',
              'expires_in_seconds': 300,
              'required_headers': {
                'Content-Type': 'image/jpeg',
                'x-storage-token': 'signed-upload-token',
                'Authorization': 'Bearer should-not-be-forwarded',
                'X-Household-Id': 'other-household',
              },
            }),
            200,
          );
        }

        expect(request.method, 'PUT');
        expect(request.url.scheme, 'https');
        expect(request.url.host, 'storage.example.net');
        expect(request.headers['authorization'], isNull);
        expect(request.headers['x-household-id'], isNull);
        expect(request.headers['x-storage-token'], 'signed-upload-token');
        return http.Response('', 201);
      });
      final uploader = HttpPhotoUpload(
        config: PhotoNetworkConfig.forApiBase(
          Uri.parse('https://api.example.net'),
        ),
        householdContext: testHouseholdContext,
        client: client,
      );
      final input = image.encodePng(image.Image(width: 1, height: 1));

      expect(await uploader.upload(input), 'opaque:external');
      expect(requests, hasLength(2));
    },
  );
  test('remote upload urls stay unchanged', () {
    final config = PhotoNetworkConfig(
      apiBase: Uri.parse('http://10.0.2.2:8000'),
      emulatorHost: '10.0.2.2',
    );
    final remote = Uri.parse('https://example.blob.core.windows.net/a?sig=abc');
    expect(config.adaptUploadUrl(remote), remote);
  });
  test('routing follows the configured development host', () {
    final local = Uri.parse('http://127.0.0.1:8000/staging/upload/token');
    final emulator = PhotoNetworkConfig.forApiBase(
      Uri.parse('http://10.0.2.2:8000'),
    );
    final device = PhotoNetworkConfig.forApiBase(
      Uri.parse('http://127.0.0.1:8000'),
    );
    expect(emulator.adaptUploadUrl(local).host, '10.0.2.2');
    expect(device.adaptUploadUrl(local), local);
    final remote = Uri.parse('https://example.blob.core.windows.net/object');
    expect(emulator.adaptUploadUrl(remote), remote);
    expect(device.adaptUploadUrl(remote), remote);
  });

  test('authorization timeout prevents a binary upload', () async {
    var calls = 0;
    final uploader = HttpPhotoUpload(
      config: PhotoNetworkConfig.forApiBase(Uri.parse('http://10.0.2.2:8000')),
      householdContext: testHouseholdContext,
      timeout: const Duration(milliseconds: 10),
      client: MockClient((_) {
        calls++;
        return Completer<http.Response>().future;
      }),
    );
    final input = image.encodePng(image.Image(width: 1, height: 1));

    await expectLater(uploader.upload(input), throwsA(isA<TimeoutException>()));
    expect(calls, 1);
  });

  test('binary upload timeout does not complete the image reference', () async {
    var calls = 0;
    final uploader = HttpPhotoUpload(
      config: PhotoNetworkConfig.forApiBase(Uri.parse('http://10.0.2.2:8000')),
      householdContext: testHouseholdContext,
      timeout: const Duration(milliseconds: 10),
      client: MockClient((_) {
        calls++;
        if (calls == 1) {
          return Future.value(
            http.Response(
              jsonEncode({
                'upload_url': 'http://127.0.0.1:8000/staging/upload/token',
                'image_ref': 'opaque:not-complete',
                'expires_in_seconds': 300,
                'required_headers': {'Content-Type': 'image/jpeg'},
              }),
              200,
            ),
          );
        }
        return Completer<http.Response>().future;
      }),
    );
    final input = image.encodePng(image.Image(width: 1, height: 1));

    await expectLater(uploader.upload(input), throwsA(isA<TimeoutException>()));
    expect(calls, 2);
  });
}
