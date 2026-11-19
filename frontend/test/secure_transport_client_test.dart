import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:nestock/features/onboarding/data/secure_transport_client.dart';

void main() {
  test(
    'rejects remote HTTP before forwarding credential-bearing requests',
    () async {
      var calls = 0;
      final client = SecureTransportClient(
        allowInsecureLoopback: true,
        innerClient: MockClient((_) async {
          calls++;
          return http.Response('{}', 200);
        }),
      );

      await expectLater(
        client.get(
          Uri.parse('http://api.example.test/households'),
          headers: const {
            'authorization': 'Bearer secret',
            'x-household-id': 'h',
          },
        ),
        throwsArgumentError,
      );
      await expectLater(
        client.post(
          Uri.parse('http://media.example.test/upload-authorization'),
          headers: const {
            'authorization': 'Bearer secret',
            'x-household-id': 'h',
          },
          body: '{}',
        ),
        throwsArgumentError,
      );
      expect(calls, 0);
    },
  );

  test('accepts HTTPS API and media destinations', () async {
    final urls = <Uri>[];
    final client = SecureTransportClient(
      allowInsecureLoopback: false,
      innerClient: MockClient((request) async {
        urls.add(request.url);
        return http.Response('{}', 200);
      }),
    );

    for (final url in [
      Uri.parse('https://api.example.test/households'),
      Uri.parse('https://media.example.test/upload-authorization'),
    ]) {
      await client.get(url, headers: const {'authorization': 'Bearer secret'});
    }

    expect(urls, hasLength(2));
  });

  test(
    'allows HTTP only for explicit loopback development endpoints',
    () async {
      final urls = <Uri>[];
      final client = SecureTransportClient(
        allowInsecureLoopback: true,
        innerClient: MockClient((request) async {
          urls.add(request.url);
          return http.Response('{}', 200);
        }),
      );

      for (final url in [
        Uri.parse('http://localhost/'),
        Uri.parse('http://127.0.0.1/'),
        Uri.parse('http://127.12.34.56/'),
        Uri.parse('http://[::1]/'),
        Uri.parse('http://10.0.2.2:8000/'),
      ]) {
        await client.get(url);
      }

      expect(urls, hasLength(5));
      expect(
        SecureUrlPolicy.isAllowed(
          Uri.parse('http://api.example.test'),
          allowInsecureLoopback: true,
        ),
        isFalse,
      );
      expect(
        SecureUrlPolicy.isAllowed(
          Uri.parse('http://localhost'),
          allowInsecureLoopback: false,
        ),
        isFalse,
      );
    },
  );

  test('rejects insecure staging PUT before forwarding credentials', () async {
    var calls = 0;
    final client = SecureTransportClient(
      allowInsecureLoopback: true,
      innerClient: MockClient((_) async {
        calls++;
        return http.Response('', 201);
      }),
    );

    await expectLater(
      client.put(
        Uri.parse('http://blob.example.test/staging/object?sig=private'),
        headers: const {
          'authorization': 'Bearer secret',
          'x-household-id': 'household-secret',
          'x-ms-blob-type': 'BlockBlob',
        },
        body: 'payload',
      ),
      throwsArgumentError,
    );
    expect(calls, 0);
  });

  test('disables redirects so credentials cannot cross origins', () async {
    var calls = 0;
    final client = SecureTransportClient(
      allowInsecureLoopback: false,
      innerClient: MockClient((request) async {
        calls++;
        expect(request.followRedirects, isFalse);
        expect(request.headers['authorization'], 'Bearer secret');
        return http.Response(
          '',
          302,
          headers: {'location': 'http://attacker.example.test/steal'},
        );
      }),
    );

    final response = await client.get(
      Uri.parse('https://api.example.test/households'),
      headers: const {'authorization': 'Bearer secret'},
    );

    expect(response.statusCode, 302);
    expect(calls, 1);
  });

  test('rejects URLs with user information without exposing URL contents', () {
    final uri = Uri.parse('https://secret-token@api.example.test/households');
    expect(
      () => SecureUrlPolicy.validate(uri, allowInsecureLoopback: false),
      throwsA(
        isA<ArgumentError>().having(
          (error) => error.toString(),
          'message',
          isNot(contains('secret-token')),
        ),
      ),
    );
  });
}
