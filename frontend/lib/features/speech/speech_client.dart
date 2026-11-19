import 'dart:io';
import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:http/http.dart' as http;
import 'package:http_parser/http_parser.dart';
import 'package:path_provider/path_provider.dart';
import 'package:record/record.dart';

import '../onboarding/domain/household_context.dart';
import '../onboarding/data/secure_transport_client.dart';

abstract interface class SpeechRecorder {
  Future<void> start();
  Future<Uint8List> stop();
  Future<void> dispose();
}

class DeviceSpeechRecorder implements SpeechRecorder {
  final AudioRecorder _recorder = AudioRecorder();
  String? _path;
  @override
  Future<void> start() async {
    if (!await _recorder.hasPermission()) throw StateError('permission');
    final dir = await getTemporaryDirectory();
    _path =
        '${dir.path}${Platform.pathSeparator}nestock_voice_${DateTime.now().microsecondsSinceEpoch}.m4a';
    await _recorder.start(
      const RecordConfig(encoder: AudioEncoder.aacLc),
      path: _path!,
    );
  }

  @override
  Future<Uint8List> stop() async {
    try {
      final path = await _recorder.stop();
      if (path == null) throw StateError('empty');
      return await File(path).readAsBytes();
    } finally {
      final path = _path;
      _path = null;
      if (path != null && await File(path).exists()) await File(path).delete();
    }
  }

  @override
  Future<void> dispose() async {
    await _recorder.dispose();
    final path = _path;
    if (path != null && await File(path).exists()) await File(path).delete();
  }
}

abstract interface class SpeechClient {
  Future<String> transcribe(Uint8List bytes);
}

class HttpSpeechClient implements SpeechClient {
  HttpSpeechClient(
    this.baseUrl, {
    required this.householdContext,
    http.Client? client,
  }) : client = SecureTransportClient(innerClient: client);
  final Uri baseUrl;
  final HouseholdRequestContext householdContext;
  final http.Client client;
  @override
  Future<String> transcribe(Uint8List bytes) async {
    if (bytes.isEmpty || bytes.length > 5 * 1024 * 1024) {
      throw StateError('invalid-size');
    }
    final request = http.MultipartRequest(
      'POST',
      baseUrl.resolve('/api/speech/transcribe'),
    );
    request.headers.addAll(householdContext.snapshot().headers);
    request.files.add(
      http.MultipartFile.fromBytes(
        'file',
        bytes,
        filename: 'recording.m4a',
        contentType: MediaType('audio', 'm4a'),
      ),
    );
    final response = await client
        .send(request)
        .timeout(const Duration(seconds: 45));
    if (response.statusCode != 200) {
      throw SpeechHttpException(response.statusCode);
    }
    final text = await response.stream.bytesToString().timeout(
      const Duration(seconds: 45),
    );
    return (jsonDecode(text) as Map<String, dynamic>)['transcript'] as String;
  }
}

class SpeechHttpException implements Exception {
  const SpeechHttpException(this.status);
  final int status;
}
