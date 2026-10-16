import 'dart:async';
import 'dart:io' show Platform;

import 'package:dio/dio.dart';
import 'package:uuid/uuid.dart';

import '../config.dart';
import 'api_exception.dart';
import 'token_store.dart';

/// A signed-in session's tokens, as the API returns them.
class TokenPair {
  const TokenPair({required this.accessToken, required this.refreshToken});

  factory TokenPair.fromJson(Map<String, dynamic> json) => TokenPair(
        accessToken: json['access_token'] as String,
        refreshToken: json['refresh_token'] as String,
      );

  final String accessToken;
  final String refreshToken;
}

/// Every call to the API goes through here.
///
/// - **Errors** come back as [ApiException], with the server's own message.
/// - **The access token** lives in memory. On a 401 the client refreshes once
///   and retries; several requests failing at once share one refresh. If the
///   refresh itself is refused, the session is over and [onSessionExpired]
///   fires.
/// - **Creates** carry an `Idempotency-Key`, so a retry after a dropped
///   connection does not post twice.
class ApiClient {
  ApiClient({
    required this._store,
    Dio? dio,
    this.onSessionExpired,
  }) : _dio = dio ??
            Dio(BaseOptions(
              baseUrl: '${AppConfig.apiBaseUrl}${AppConfig.apiPrefix}',
              connectTimeout: const Duration(seconds: 10),
              receiveTimeout: const Duration(seconds: 20),
              sendTimeout: const Duration(seconds: 20),
              // Statuses are handled here, not thrown by Dio, so every error
              // takes the same path into an ApiException.
              validateStatus: (_) => true,
              responseType: ResponseType.json,
            ));

  final TokenStore _store;
  final Dio _dio;
  void Function()? onSessionExpired;

  String? _accessToken;
  Future<bool>? _refreshing;

  bool get hasAccessToken => _accessToken != null;

  Future<void> startSession(TokenPair tokens) async {
    _accessToken = tokens.accessToken;
    await _store.writeRefreshToken(tokens.refreshToken);
  }

  Future<void> endSession() async {
    _accessToken = null;
    await _store.clearRefreshToken();
  }

  /// What the server is told about this device at sign-in and refresh.
  Future<Map<String, dynamic>> deviceFields() async => {
        'device_platform': _platform(),
        'device_id': await _store.installationId(),
      };

  Future<dynamic> get(String path, {Map<String, dynamic>? query}) =>
      _request('GET', path, query: query);

  Future<dynamic> post(String path, {Object? body, bool idempotent = false, bool auth = true}) =>
      _request('POST', path, body: body, idempotent: idempotent, auth: auth);

  Future<dynamic> put(String path, {Object? body}) => _request('PUT', path, body: body);

  Future<dynamic> patch(String path, {Object? body}) => _request('PATCH', path, body: body);

  Future<dynamic> delete(String path) => _request('DELETE', path);

  /// Restores a session from the stored refresh token, at app start. False when
  /// there is none, or it is no longer good.
  Future<bool> resume() => _refresh();

  Future<dynamic> _request(
    String method,
    String path, {
    Object? body,
    Map<String, dynamic>? query,
    bool idempotent = false,
    bool auth = true,
  }) async {
    // One key per logical request, reused on the retry after a refresh: the
    // server must see the retry as the same request.
    final idempotencyKey = idempotent ? const Uuid().v4() : null;

    Future<Response<dynamic>> send() => _send(method, path,
        body: body, query: query, idempotencyKey: idempotencyKey, auth: auth);

    var response = await send();
    if (response.statusCode == 401 && auth) {
      if (await _refresh()) {
        response = await send();
      } else {
        onSessionExpired?.call();
      }
    }
    final status = response.statusCode ?? 0;
    if (status >= 400) {
      throw ApiException.fromResponse(
        status,
        response.data,
        retryAfter: int.tryParse(response.headers.value('retry-after') ?? ''),
      );
    }
    return response.data;
  }

  Future<Response<dynamic>> _send(
    String method,
    String path, {
    Object? body,
    Map<String, dynamic>? query,
    String? idempotencyKey,
    bool auth = true,
  }) async {
    final headers = <String, dynamic>{
      if (auth && _accessToken != null) 'Authorization': 'Bearer $_accessToken',
      'Idempotency-Key': ?idempotencyKey,
    };
    try {
      return await _dio.request<dynamic>(
        path,
        data: body,
        queryParameters: query,
        options: Options(method: method, headers: headers),
      );
    } on DioException {
      throw const ApiException.network();
    }
  }

  Future<bool> _refresh() {
    return _refreshing ??= _doRefresh().whenComplete(() => _refreshing = null);
  }

  Future<bool> _doRefresh() async {
    final refreshToken = await _store.readRefreshToken();
    if (refreshToken == null) return false;
    final response = await _send(
      'POST',
      '/auth/refresh',
      body: {'refresh_token': refreshToken, ...await deviceFields()},
      auth: false,
    );
    if (response.statusCode == 200 && response.data is Map) {
      await startSession(TokenPair.fromJson(Map<String, dynamic>.from(response.data as Map)));
      return true;
    }
    if (response.statusCode == 401 || response.statusCode == 403) {
      // The refresh token is spent, revoked or banned: the session is over.
      await endSession();
    }
    return false;
  }

  static String _platform() {
    try {
      if (Platform.isAndroid) return 'android';
      if (Platform.isIOS) return 'ios';
    } catch (_) {
      // Not available in some test environments.
    }
    return 'other';
  }
}
