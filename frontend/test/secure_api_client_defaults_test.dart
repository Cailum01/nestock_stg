import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:image/image.dart' as image;
import 'package:nestock/features/agent/data/http_agent_client.dart';
import 'package:nestock/features/inventory/inventory_view_client.dart';
import 'package:nestock/features/onboarding/data/identity_api.dart';
import 'package:nestock/features/onboarding/data/secure_transport_client.dart';
import 'package:nestock/features/photo/photo_upload.dart';
import 'package:nestock/features/shopping/shopping_view_client.dart';
import 'package:nestock/features/speech/speech_client.dart';

import 'household_test_support.dart';

typedef _ClientAction = Future<void> Function(http.Client client);

final _remoteBaseUrl = Uri.parse('http://api.example.test');
final _loopbackBaseUrl = Uri.parse('http://localhost:8000');
final _photoBytes = image.encodeJpg(image.Image(width: 1, height: 1));

List<({String name, _ClientAction action})> _clientActions(Uri baseUrl) => [
  (
    name: 'agent',
    action: (client) async {
      await HttpAgentClient(
        baseUrl: baseUrl,
        householdContext: testHouseholdContext,
        client: client,
      ).send('test');
    },
  ),
  (
    name: 'inventory',
    action: (client) async {
      await HttpInventoryViewClient(
        baseUrl: baseUrl,
        householdContext: testHouseholdContext,
        client: client,
      ).load();
    },
  ),
  (
    name: 'shopping',
    action: (client) async {
      await HttpShoppingViewClient(
        baseUrl: baseUrl,
        householdContext: testHouseholdContext,
        client: client,
      ).load();
    },
  ),
  (
    name: 'photo upload',
    action: (client) async {
      await HttpPhotoUpload(
        config: PhotoNetworkConfig(apiBase: baseUrl),
        householdContext: testHouseholdContext,
        client: client,
      ).upload(_photoBytes);
    },
  ),
  (
    name: 'speech',
    action: (client) async {
      await HttpSpeechClient(
        baseUrl,
        householdContext: testHouseholdContext,
        client: client,
      ).transcribe(Uint8List.fromList([1]));
    },
  ),
  (
    name: 'identity',
    action: (client) async {
      await IdentityApi(
        baseUrl: baseUrl,
        client: client,
      ).bootstrap('challenge');
    },
  ),
];

http.Response _responseFor(http.BaseRequest request) {
  if (request.url.path == '/api/media/upload-authorization') {
    return http.Response(
      '{"image_ref":"image-ref","upload_url":"http://localhost:8000/upload",'
      '"required_headers":{}}',
      200,
    );
  }
  if (request.url.path == '/upload') return http.Response('', 201);
  if (request.url.path == '/identity/bootstrap') {
    return http.Response('{"bearer_credential":"test-token"}', 200);
  }
  if (request.url.path == '/api/speech/transcribe') {
    return http.Response('{"transcript":"hello"}', 200);
  }
  if (request.url.path == '/agent/messages') {
    return http.Response('{"message":"ok"}', 200);
  }
  return http.Response('[]', 200);
}

void main() {
  group('credential-bearing API client transport defaults', () {
    test('default constructors reject remote HTTP destinations', () async {
      final defaultClients = <({String name, http.Client client})>[
        (
          name: 'agent',
          client: HttpAgentClient(
            baseUrl: _remoteBaseUrl,
            householdContext: testHouseholdContext,
          ).client,
        ),
        (
          name: 'inventory',
          client: HttpInventoryViewClient(
            baseUrl: _remoteBaseUrl,
            householdContext: testHouseholdContext,
          ).client,
        ),
        (
          name: 'shopping',
          client: HttpShoppingViewClient(
            baseUrl: _remoteBaseUrl,
            householdContext: testHouseholdContext,
          ).client,
        ),
        (
          name: 'photo upload',
          client: HttpPhotoUpload(
            config: PhotoNetworkConfig(apiBase: _remoteBaseUrl),
            householdContext: testHouseholdContext,
          ).client,
        ),
        (
          name: 'speech',
          client: HttpSpeechClient(
            _remoteBaseUrl,
            householdContext: testHouseholdContext,
          ).client,
        ),
      ];

      for (final entry in defaultClients) {
        expect(entry.client, isA<SecureTransportClient>(), reason: entry.name);
        expect(
          () => entry.client.send(
            http.Request('GET', _remoteBaseUrl)
              ..headers['Authorization'] = 'Bearer test-token'
              ..headers['X-Household-Id'] = 'test-household',
          ),
          throwsArgumentError,
          reason: entry.name,
        );
      }

      await expectLater(
        IdentityApi(baseUrl: _remoteBaseUrl).bootstrap('challenge'),
        throwsArgumentError,
        reason: 'identity',
      );
    });

    test('injected clients cannot bypass remote HTTP rejection', () async {
      for (final entry in _clientActions(_remoteBaseUrl)) {
        var forwarded = 0;
        final injectedClient = MockClient((request) async {
          forwarded++;
          return _responseFor(request);
        });

        await expectLater(
          entry.action(injectedClient),
          throwsArgumentError,
          reason: entry.name,
        );
        expect(forwarded, 0, reason: entry.name);
      }
    });

    test(
      'injected clients allow loopback development and disable redirects',
      () async {
        final forwarded = <http.BaseRequest>[];
        final injectedClient = MockClient((request) async {
          forwarded.add(request);
          expect(request.followRedirects, isFalse);
          return _responseFor(request);
        });

        for (final entry in _clientActions(_loopbackBaseUrl)) {
          await entry.action(injectedClient);
        }

        expect(forwarded, hasLength(7));
        expect(
          forwarded.where(
            (request) =>
                request.url.path != '/identity/bootstrap' &&
                request.url.path != '/upload',
          ),
          everyElement(
            predicate<http.BaseRequest>(
              (request) =>
                  request.headers['Authorization'] ==
                      'Bearer test-install-token' &&
                  request.headers['X-Household-Id'] == 'test-household',
            ),
          ),
        );
        expect(
          forwarded.where((request) => request.url.path == '/upload'),
          everyElement(
            predicate<http.BaseRequest>(
              (request) =>
                  request.headers['Authorization'] ==
                      'Bearer test-install-token' &&
                  request.headers['X-Household-Id'] == 'test-household',
            ),
          ),
        );
      },
    );
  });
}
