/// Future HTTP adapters may consume this; widgets and offline mocks do not.
class AppConfig {
  const AppConfig({required this.apiBaseUrl});
  final String apiBaseUrl;

  static const local = AppConfig(
    apiBaseUrl: String.fromEnvironment(
      'API_BASE_URL',
      defaultValue: 'http://10.0.2.2:8000',
    ),
  );
}
