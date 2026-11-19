class AppConfig {
  const AppConfig({required this.apiBaseUrl, required this.mediaApiBaseUrl});

  final String apiBaseUrl;
  final String mediaApiBaseUrl;

  /// Values are compile-time so a published app cannot silently change hosts.
  /// Identity and household APIs use `API_BASE_URL`; upload authorization and
  /// speech use `MEDIA_API_BASE`.
  /// Android secure-storage data survives app updates, but disabling backup
  /// means restore/reinstall requires a fresh bootstrap or recovery code.
  static const fromEnvironment = AppConfig(
    apiBaseUrl: String.fromEnvironment('API_BASE_URL', defaultValue: ''),
    mediaApiBaseUrl: String.fromEnvironment('MEDIA_API_BASE', defaultValue: ''),
  );
}
