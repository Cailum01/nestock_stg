/// A2 seam: capture/selection and upload lifecycle remain undefined.
enum PhotoAccess { unavailable, denied }

abstract interface class PhotoClient {
  Future<PhotoAccess> open();
}

class UnavailablePhotoClient implements PhotoClient {
  const UnavailablePhotoClient();
  @override
  Future<PhotoAccess> open() async => PhotoAccess.unavailable;
}
