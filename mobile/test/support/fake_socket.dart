import 'dart:async';
import 'dart:convert';

import 'package:web_socket_channel/web_socket_channel.dart';

/// A socket the test plays the server for.
class FakeSocket implements WebSocketChannel {
  FakeSocket(this.headers);

  final Map<String, dynamic> headers;
  final _incoming = StreamController<dynamic>();
  late final FakeSocketSink _sink = FakeSocketSink();
  int? _closeCode;

  /// Frames the client sent, decoded.
  List<Map<String, dynamic>> get sent => _sink.frames;
  bool get closedByClient => _sink.closed;

  /// The server sends a frame.
  void push(String type, [Map<String, dynamic> data = const {}]) =>
      _incoming.add(jsonEncode({'type': type, 'data': data}));

  void sendReady({double heartbeatSeconds = 25, DateTime? expiresAt}) => push('ready', {
        'user_id': 'u1',
        'heartbeat_seconds': heartbeatSeconds,
        'expires_at': (expiresAt ?? DateTime.now().add(const Duration(minutes: 15))).toUtc().toIso8601String(),
      });

  /// The server closes the socket.
  Future<void> serverClose(int code) {
    _closeCode = code;
    return _incoming.close();
  }

  @override
  Stream<dynamic> get stream => _incoming.stream;

  @override
  WebSocketSink get sink => _sink;

  @override
  Future<void> get ready => Future.value();

  @override
  int? get closeCode => _closeCode;

  @override
  String? get closeReason => null;

  @override
  String? get protocol => null;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

class FakeSocketSink implements WebSocketSink {
  final frames = <Map<String, dynamic>>[];
  bool closed = false;

  @override
  void add(dynamic data) => frames.add(Map<String, dynamic>.from(jsonDecode(data as String) as Map));

  @override
  Future<void> close([int? closeCode, String? closeReason]) async => closed = true;

  @override
  dynamic noSuchMethod(Invocation invocation) => super.noSuchMethod(invocation);
}

/// Records every socket the client opens.
class FakeSockets {
  final opened = <FakeSocket>[];

  FakeSocket get last => opened.last;

  WebSocketChannel connect(Uri uri, Map<String, dynamic> headers) {
    final socket = FakeSocket(headers);
    opened.add(socket);
    return socket;
  }
}
