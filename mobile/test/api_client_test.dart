import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/api_exception.dart';
import 'package:myradius/core/api/token_store.dart';

import 'support/fake_adapter.dart';

void main() {
  late MemoryTokenStore store;

  setUp(() => store = MemoryTokenStore()..refreshToken = 'refresh-1');

  test('an expired access token is refreshed once and the request retried', () async {
    final adapter = FakeAdapter((request) {
      if (request.path == '/auth/refresh') return FakeReply(200, tokens('access-2', 'refresh-2'));
      final auth = request.headers['Authorization'];
      return auth == 'Bearer access-2'
          ? const FakeReply(200, {'ok': true})
          : FakeReply(401, errorBody('unauthenticated', 'The access token has expired'));
    });
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    await api.startSession(const TokenPair(accessToken: 'access-1', refreshToken: 'refresh-1'));

    expect(await api.get('/me'), {'ok': true});
    expect(adapter.requests.map((r) => r.path), ['/me', '/auth/refresh', '/me']);
    expect(store.refreshToken, 'refresh-2');
  });

  test('requests failing together share one refresh', () async {
    final adapter = FakeAdapter((request) {
      if (request.path == '/auth/refresh') return FakeReply(200, tokens('access-2', 'refresh-2'));
      return request.headers['Authorization'] == 'Bearer access-2'
          ? const FakeReply(200, {'ok': true})
          : FakeReply(401, errorBody('unauthenticated', 'expired'));
    });
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    await api.startSession(const TokenPair(accessToken: 'access-1', refreshToken: 'refresh-1'));

    await Future.wait([api.get('/me'), api.get('/feed'), api.get('/me/radius')]);
    expect(adapter.requests.where((r) => r.path == '/auth/refresh'), hasLength(1));
  });

  test('a refused refresh ends the session', () async {
    var expired = false;
    final adapter = FakeAdapter((request) =>
        FakeReply(401, errorBody('unauthenticated', 'That session is no longer valid')));
    final api = ApiClient(store: store, dio: fakeDio(adapter), onSessionExpired: () => expired = true);
    await api.startSession(const TokenPair(accessToken: 'access-1', refreshToken: 'refresh-1'));

    await expectLater(api.get('/me'), throwsA(isA<ApiException>()));
    expect(expired, isTrue);
    expect(store.refreshToken, isNull);
    expect(api.hasAccessToken, isFalse);
  });

  test("the server's error envelope becomes an ApiException with its message", () async {
    final adapter = FakeAdapter((_) => FakeReply(
          429,
          errorBody('rate_limited', 'A code was sent recently. Please wait before asking for another.'),
          {'retry-after': ['42']},
        ));
    final api = ApiClient(store: store, dio: fakeDio(adapter));

    final error = await api
        .post('/auth/otp/request', body: {'phone_number': '+254712345678'}, auth: false)
        .then<ApiException?>((_) => null, onError: (Object e) => e as ApiException);
    expect(error!.code, 'rate_limited');
    expect(error.message, startsWith('A code was sent recently'));
    expect(error.retryAfterSeconds, 42);
    expect(error.isRateLimited, isTrue);
  });

  test('a create keeps its idempotency key across the retry', () async {
    final adapter = FakeAdapter((request) {
      if (request.path == '/auth/refresh') return FakeReply(200, tokens('access-2', 'refresh-2'));
      return request.headers['Authorization'] == 'Bearer access-2'
          ? const FakeReply(201, {'id': 'p1'})
          : FakeReply(401, errorBody('unauthenticated', 'expired'));
    });
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    await api.startSession(const TokenPair(accessToken: 'access-1', refreshToken: 'refresh-1'));

    await api.post('/posts', body: {'body': 'Hello'}, idempotent: true);
    final keys = adapter.requests
        .where((r) => r.path == '/posts')
        .map((r) => r.headers['Idempotency-Key'])
        .toList();
    expect(keys, hasLength(2));
    expect(keys.first, isNotNull);
    expect(keys.first, keys.last);
  });

  test('sign-in sends the installation ID, never a token', () async {
    final adapter = FakeAdapter((_) => const FakeReply(200, {}));
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    final fields = await api.deviceFields();
    expect(fields['device_id'], store.id);
    expect(fields.keys, containsAll(['device_id', 'device_platform']));
  });
}
