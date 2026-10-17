import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/api_exception.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/feed/feed_controller.dart';
import 'package:myradius/features/feed/feed_models.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> postJson(
  String id, {
  String body = 'Water off on Saturday',
  String category = 'alert',
  int reactions = 0,
  String? myReaction,
  int comments = 0,
  String status = 'published',
  String authorId = 'u2',
}) =>
    {
      'id': id,
      'author': {'id': authorId, 'display_name': 'Wanjiku M.', 'is_verified_neighbour': true},
      'body': body,
      'category': category,
      'images': <Object>[],
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'distance_bucket': 'under_1km',
      'comment_count': comments,
      'reaction_count': reactions,
      'my_reaction': myReaction,
      'status': status,
      'created_at': '2026-10-17T08:00:00Z',
    };

Map<String, dynamic> feedJson(List<Map<String, dynamic>> items, {String? next}) =>
    {'items': items, 'next_cursor': next, 'radius_km': 15};

/// Waits for the fake API's replies to arrive.
Future<void> until(bool Function() done) async {
  for (var i = 0; i < 200 && !done(); i++) {
    await Future<void>.delayed(const Duration(milliseconds: 1));
  }
  expect(done(), isTrue, reason: 'timed out waiting');
}

void main() {
  group('models', () {
    test('distance is only ever a band', () {
      expect(distanceLabel('under_1km'), 'under 1 km');
      expect(distanceLabel('3_10km'), '3–10 km');
      expect(distanceLabel(null), isNull);
      expect(distanceLabel('412m'), isNull);
    });

    test('a photo is served at the smallest size that fills the space', () {
      const image = PostImage(mediaId: 'm', urlsByWidth: {320: 'a', 1080: 'c', 640: 'b'});
      expect(image.urlFor(300), 'a');
      expect(image.urlFor(600), 'b');
      expect(image.urlFor(700), 'c');
      expect(image.urlFor(2000), 'c');
    });

    test('times read briefly', () {
      final now = DateTime.utc(2026, 10, 17, 12);
      expect(timeAgo(now.subtract(const Duration(seconds: 20)), now: now), 'now');
      expect(timeAgo(now.subtract(const Duration(minutes: 20)), now: now), '20 min');
      expect(timeAgo(now.subtract(const Duration(hours: 3)), now: now), '3 h');
      expect(timeAgo(DateTime.utc(2026, 9, 2, 12), now: now), '2 Sep');
    });

    test('a post reads from the API and knows when it is held', () {
      final post = Post.fromJson(postJson('p1', status: 'hidden_pending_review'));
      expect(post.author.initials, 'WM');
      expect(post.categoryLabel, 'Alert');
      expect(post.areaLabel.label, 'Nyeri Town');
      expect(post.isHeld, isTrue);
      expect(post.copyWith(myReaction: 'helpful').copyWith(clearReaction: true).myReaction, isNull);
    });
  });

  group('controller', () {
    late FakeAdapter adapter;
    late ProviderContainer container;
    late FakeReply Function(RequestOptions request) handler;

    setUp(() async {
      handler = (request) => FakeReply(404, errorBody('not_found', 'Resource not found'));
      adapter = FakeAdapter((request) => handler(request));
      final store = MemoryTokenStore();
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      await api.startSession(const TokenPair(accessToken: 'a1', refreshToken: 'r1'));
      container = ProviderContainer(overrides: [
        tokenStoreProvider.overrideWithValue(store),
        apiClientProvider.overrideWithValue(api),
      ]);
      addTearDown(container.dispose);
    });

    FeedState state() => container.read(feedControllerProvider);

    test('loads the first page, then more, without repeats', () async {
      handler = (request) => request.queryParameters['cursor'] == 'c2'
          ? FakeReply(200, feedJson([postJson('p2'), postJson('p3')]))
          : FakeReply(200, feedJson([postJson('p1'), postJson('p2')], next: 'c2'));
      container.listen(feedControllerProvider, (_, _) {});
      await until(() => !state().loading);
      expect(state().posts.map((p) => p.id), ['p1', 'p2']);
      expect(state().radiusKm, 15);
      expect(state().hasMore, isTrue);

      await container.read(feedControllerProvider.notifier).loadMore();
      expect(state().posts.map((p) => p.id), ['p1', 'p2', 'p3']);
      expect(state().hasMore, isFalse);
    });

    test('helpful shows at once and is put back if the server refuses', () async {
      var refuse = false;
      handler = (request) => switch ((request.method, request.path)) {
            ('GET', '/feed') => FakeReply(200, feedJson([postJson('p1', reactions: 2)])),
            ('PUT', '/posts/p1/reaction') => refuse
                ? FakeReply(403, errorBody('restricted', "You can't react right now"))
                : const FakeReply(200, {'reaction_count': 3}),
            ('DELETE', '/posts/p1/reaction') => const FakeReply(204),
            _ => FakeReply(404, errorBody('not_found', 'Resource not found')),
          };
      container.listen(feedControllerProvider, (_, _) {});
      await until(() => !state().loading);
      final controller = container.read(feedControllerProvider.notifier);

      await controller.toggleHelpful('p1');
      expect(state().posts.single.myReaction, 'helpful');
      expect(state().posts.single.reactionCount, 3);
      expect(adapter.requests.last.data, {'kind': 'helpful'});

      await controller.toggleHelpful('p1');
      expect(state().posts.single.myReaction, isNull);
      expect(state().posts.single.reactionCount, 2);

      refuse = true;
      await expectLater(controller.toggleHelpful('p1'), throwsA(isA<ApiException>()));
      expect(state().posts.single.myReaction, isNull);
      expect(state().posts.single.reactionCount, 2);
    });

    test('a failed first load is an error, and a new post goes on top', () async {
      handler = (request) => FakeReply(503, errorBody('unavailable', 'Try again shortly'));
      container.listen(feedControllerProvider, (_, _) {});
      await until(() => !state().loading);
      expect(state().error?.message, 'Try again shortly');

      final controller = container.read(feedControllerProvider.notifier);
      controller.prepend(Post.fromJson(postJson('mine', authorId: 'u1')));
      controller.commentAdded('mine');
      expect(state().posts.single.commentCount, 1);
    });
  });

  group('screen', () {
    void usePhoneScreen(WidgetTester tester) {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
    }

    testWidgets('a signed-in neighbour sees the feed and marks a post helpful', (tester) async {
      usePhoneScreen(tester);
      final store = MemoryTokenStore()..refreshToken = 'r1';
      final adapter = FakeAdapter((request) => switch ((request.method, request.path)) {
            ('POST', '/auth/refresh') => FakeReply(200, tokens('a1', 'r2')),
            ('GET', '/me') => const FakeReply(200, {
                'id': 'u1',
                'display_name': 'Amina K.',
                'status': 'active',
                'has_home_location': true,
                'is_verified_neighbour': false,
                'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
              }),
            ('GET', '/feed') => FakeReply(200, feedJson([
                postJson('p1', body: 'Does anyone know a fundi for a leaking tank?', category: 'question'),
              ])),
            ('PUT', '/posts/p1/reaction') => const FakeReply(200, {'reaction_count': 1}),
            _ => FakeReply(404, errorBody('not_found', 'Resource not found')),
          });
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      await tester.pumpWidget(ProviderScope(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(api),
          realtimeProvider.overrideWith((ref) {
            final client = RealtimeClient(api, connect: FakeSockets().connect, uri: Uri.parse('ws://api.test/v1/ws'));
            ref.onDispose(client.dispose);
            return client;
          }),
        ],
        child: const MyRadiusApp(),
      ));
      await tester.pumpAndSettle();

      expect(find.text('Nyeri Town'), findsWidgets);
      expect(find.text('Within 15 km of home'), findsOneWidget);
      expect(find.text('Does anyone know a fundi for a leaking tank?'), findsOneWidget);
      expect(find.text('Question'), findsOneWidget);
      expect(find.textContaining('under 1 km'), findsOneWidget);

      await tester.tap(find.text('0 helpful'));
      await tester.pumpAndSettle();
      expect(find.text('1 helpful'), findsOneWidget);
    });
  });
}
