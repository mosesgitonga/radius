import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:uuid/uuid.dart';

/// Where the long-lived secrets live: the refresh token, and this installation's
/// random ID. The access token is short-lived and is kept in memory only.
abstract class TokenStore {
  Future<String?> readRefreshToken();
  Future<void> writeRefreshToken(String token);
  Future<void> clearRefreshToken();

  /// A random ID made once per installation and kept. The server stores only a
  /// keyed hash of it, so a ban can reach a device; it identifies nothing else.
  Future<String> installationId();
}

/// The Android keystore (and the iOS keychain), via flutter_secure_storage.
class SecureTokenStore implements TokenStore {
  SecureTokenStore([FlutterSecureStorage? storage])
      : _storage = storage ?? const FlutterSecureStorage();

  final FlutterSecureStorage _storage;

  static const _refreshKey = 'refresh_token';
  static const _installationKey = 'installation_id';

  @override
  Future<String?> readRefreshToken() => _storage.read(key: _refreshKey);

  @override
  Future<void> writeRefreshToken(String token) =>
      _storage.write(key: _refreshKey, value: token);

  @override
  Future<void> clearRefreshToken() => _storage.delete(key: _refreshKey);

  @override
  Future<String> installationId() async {
    final existing = await _storage.read(key: _installationKey);
    if (existing != null && existing.isNotEmpty) return existing;
    final created = const Uuid().v4();
    await _storage.write(key: _installationKey, value: created);
    return created;
  }
}

/// In memory, for tests.
class MemoryTokenStore implements TokenStore {
  String? refreshToken;
  String id = 'test-installation-0001';

  @override
  Future<String?> readRefreshToken() async => refreshToken;

  @override
  Future<void> writeRefreshToken(String token) async => refreshToken = token;

  @override
  Future<void> clearRefreshToken() async => refreshToken = null;

  @override
  Future<String> installationId() async => id;
}
