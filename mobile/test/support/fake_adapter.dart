import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';

/// One scripted reply.
class FakeReply {
  const FakeReply(this.status, [this.body, this.headers = const {}]);
  final int status;
  final Object? body;
  final Map<String, List<String>> headers;
}

/// A Dio adapter that answers from a script and records what was asked.
class FakeAdapter implements HttpClientAdapter {
  FakeAdapter(this.handler);

  /// Given the request, returns the reply.
  final FakeReply Function(RequestOptions request) handler;
  final List<RequestOptions> requests = [];

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    requests.add(options);
    final reply = handler(options);
    return ResponseBody.fromString(
      reply.body == null ? '' : jsonEncode(reply.body),
      reply.status,
      headers: {
        Headers.contentTypeHeader: [Headers.jsonContentType],
        ...reply.headers,
      },
    );
  }

  @override
  void close({bool force = false}) {}
}

Dio fakeDio(FakeAdapter adapter) {
  final dio = Dio(BaseOptions(baseUrl: 'http://api.test/v1', validateStatus: (_) => true));
  dio.httpClientAdapter = adapter;
  return dio;
}

Map<String, dynamic> tokens(String access, String refresh) => {
      'access_token': access,
      'refresh_token': refresh,
      'token_type': 'bearer',
      'expires_in_seconds': 900,
    };

Map<String, dynamic> errorBody(String code, String message) => {
      'error': {'code': code, 'message': message, 'details': <String, dynamic>{}},
    };
