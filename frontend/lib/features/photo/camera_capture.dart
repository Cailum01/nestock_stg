import 'dart:typed_data';

import 'package:image_picker/image_picker.dart';

abstract interface class CameraCapture {
  Future<Uint8List?> capture();
}

class DeviceCameraCapture implements CameraCapture {
  const DeviceCameraCapture();
  @override
  Future<Uint8List?> capture() async {
    final photo = await ImagePicker().pickImage(source: ImageSource.camera);
    return photo?.readAsBytes();
  }
}
