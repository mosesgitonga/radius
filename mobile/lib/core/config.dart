/// Build-time configuration.
///
/// The API's address comes from `--dart-define=API_BASE_URL=...`. The default is
/// the development backend as an Android emulator sees it: `10.0.2.2` is the
/// emulator's name for the host machine. A phone on the same Wi-Fi needs the
/// PC's address instead, e.g. `--dart-define=API_BASE_URL=http://192.168.1.20:8000`.
class AppConfig {
  const AppConfig._();

  static const apiBaseUrl = String.fromEnvironment(
    'API_BASE_URL',
    defaultValue: 'http://10.0.2.2:8000',
  );

  static const apiPrefix = '/v1';

  /// Kenya's calling code. Numbers are typed without it and sent in E.164.
  static const countryCode = '+254';
}
