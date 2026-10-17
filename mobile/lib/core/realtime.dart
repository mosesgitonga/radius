import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:flutter/foundation.dart';
import 'package:web_socket_channel/io.dart';
import 'package:web_socket_channel/web_socket_channel.dart';

import 'api/api_client.dart';
import 'config.dart';

/// One frame from the server: `{"type": ..., "data": {...}}`.
class RealtimeEvent {
  const RealtimeEvent(this.type, this.data);

  /// Sent by the client itself each time a connection is (re)established, so
  /// listeners can catch up on anything missed while it was down.
  static const connected = 'ready';

  final String type;
  final Map<String, dynamic> data;

  String? get conversationId => data['conversation_id'] as String?;
}

/// Opens a socket. Swapped for a fake in tests.
typedef SocketConnector = WebSocketChannel Function(Uri uri, Map<String, dynamic> headers);

WebSocketChannel _connectIo(Uri uri, Map<String, dynamic> headers) =>
    IOWebSocketChannel.connect(uri, headers: headers, connectTimeout: const Duration(seconds: 10));

/// The realtime socket, `/v1/ws`.
///
/// It only rings the doorbell: events say *that* something changed, and
/// listeners fetch what changed from the API. So a missed event costs nothing
/// a reconnect does not repair.
///
/// - **Sign-in:** the access token goes in the `Authorization` header, never
///   the URL. Before it expires a fresh one is sent as an `auth` frame.
/// - **Staying up:** a ping each heartbeat; reconnects with backoff, and at once
///   when the app returns to the foreground. A 4401 close renews the sign-in
///   first; a 4403 (banned) stops for good.
class RealtimeClient {
  RealtimeClient(this._api, {SocketConnector? connect, Uri? uri, Random? random})
      : _connect = connect ?? _connectIo,
        _uri = uri ?? defaultUri(),
        _random = random ?? Random();

  static Uri defaultUri() {
    final base = Uri.parse('${AppConfig.apiBaseUrl}${AppConfig.apiPrefix}/ws');
    return base.replace(scheme: base.scheme == 'https' ? 'wss' : 'ws');
  }

  static const closeUnauthenticated = 4401;
  static const closeBanned = 4403;
  static const maxBackoff = Duration(seconds: 30);

  final ApiClient _api;
  final SocketConnector _connect;
  final Uri _uri;
  final Random _random;

  final _events = StreamController<RealtimeEvent>.broadcast();
  final ValueNotifier<bool> connected = ValueNotifier(false);

  WebSocketChannel? _channel;
  StreamSubscription<dynamic>? _subscription;
  Timer? _pingTimer;
  Timer? _renewTimer;
  Timer? _retryTimer;
  bool _wanted = false;
  bool _connecting = false;
  bool _renewFirst = false;
  int _failures = 0;

  Stream<RealtimeEvent> get events => _events.stream;

  /// Connects, and keeps reconnecting until [stop].
  void start() {
    if (_wanted) return;
    _wanted = true;
    _open();
  }

  /// Back in the foreground: if the connection dropped meanwhile, try now
  /// rather than waiting out the backoff.
  void resume() {
    if (!_wanted || _channel != null || _connecting) return;
    _retryTimer?.cancel();
    _failures = 0;
    _open();
  }

  void stop() {
    _wanted = false;
    _retryTimer?.cancel();
    _teardown();
  }

  void dispose() {
    stop();
    _events.close();
    connected.dispose();
  }

  Future<void> _open() async {
    if (!_wanted || _channel != null || _connecting) return;
    _connecting = true;
    try {
      var token = _renewFirst ? null : _api.accessToken;
      token ??= await _api.renewAccessToken();
      _renewFirst = false;
      if (!_wanted) return;
      if (token == null) {
        // Signed out meanwhile, or offline: the API client reports the first,
        // and the backoff covers the second.
        _scheduleRetry();
        return;
      }
      final channel = _connect(_uri, {'Authorization': 'Bearer $token'});
      await channel.ready;
      if (!_wanted) {
        await channel.sink.close();
        return;
      }
      _channel = channel;
      _subscription = channel.stream.listen(
        _onFrame,
        onDone: () => _onClosed(channel.closeCode),
        onError: (_) {},
        cancelOnError: false,
      );
    } catch (_) {
      _scheduleRetry();
    } finally {
      _connecting = false;
    }
  }

  void _onFrame(dynamic raw) {
    final Map<String, dynamic> frame;
    try {
      frame = Map<String, dynamic>.from(jsonDecode(raw as String) as Map);
    } catch (_) {
      return;
    }
    final type = frame['type'] as String? ?? '';
    final data = frame['data'] is Map ? Map<String, dynamic>.from(frame['data'] as Map) : <String, dynamic>{};
    switch (type) {
      case 'ready':
        _failures = 0;
        final wasConnected = connected.value;
        connected.value = true;
        _schedulePing(data['heartbeat_seconds']);
        _scheduleRenewal(data['expires_at']);
        // A `ready` also answers each renewal; only a new connection means
        // there may be something to catch up on.
        if (!wasConnected) _events.add(RealtimeEvent(RealtimeEvent.connected, data));
      case 'pong':
        break;
      default:
        _events.add(RealtimeEvent(type, data));
    }
  }

  void _onClosed(int? code) {
    _teardown();
    if (!_wanted) return;
    if (code == closeBanned) {
      _wanted = false;
      return;
    }
    if (code == closeUnauthenticated) _renewFirst = true;
    _scheduleRetry();
  }

  void _send(Map<String, dynamic> frame) {
    try {
      _channel?.sink.add(jsonEncode(frame));
    } catch (_) {
      // The close handler deals with a dead socket.
    }
  }

  void _schedulePing(Object? heartbeatSeconds) {
    _pingTimer?.cancel();
    final seconds = (heartbeatSeconds as num?)?.toDouble() ?? 25;
    _pingTimer = Timer.periodic(
      Duration(milliseconds: (seconds * 1000).round()),
      (_) => _send({'type': 'ping'}),
    );
  }

  /// A minute before the token expires, send a fresh one.
  void _scheduleRenewal(Object? expiresAt) {
    _renewTimer?.cancel();
    final expiry = DateTime.tryParse('${expiresAt ?? ''}');
    if (expiry == null) return;
    var wait = expiry.difference(DateTime.now()) - const Duration(seconds: 60);
    if (wait.isNegative) wait = Duration.zero;
    _renewTimer = Timer(wait, () async {
      final token = await _api.renewAccessToken();
      if (token != null) _send({'type': 'auth', 'token': token});
    });
  }

  /// 1, 2, 4 … 30 seconds, each with up to a quarter more at random, so phones
  /// that lost the same server do not all come back at once.
  void _scheduleRetry() {
    if (!_wanted) return;
    _retryTimer?.cancel();
    final base = min(maxBackoff.inMilliseconds, 1000 * pow(2, _failures).toInt());
    _failures = min(_failures + 1, 10);
    final delay = Duration(milliseconds: base + _random.nextInt(base ~/ 4 + 1));
    _retryTimer = Timer(delay, _open);
  }

  void _teardown() {
    _pingTimer?.cancel();
    _renewTimer?.cancel();
    _subscription?.cancel();
    _subscription = null;
    final channel = _channel;
    _channel = null;
    if (channel != null) {
      try {
        channel.sink.close();
      } catch (_) {}
    }
    if (connected.value) connected.value = false;
  }
}
