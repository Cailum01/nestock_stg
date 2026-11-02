import 'dart:typed_data';

import 'package:nestock/features/photo/camera_capture.dart';

class FakeCamera implements CameraCapture {
  int calls = 0;
  bool fail = false;
  @override
  Future<Uint8List?> capture() async {
    calls++;
    if (fail) throw StateError('camera unavailable');
    return null;
  }
}
