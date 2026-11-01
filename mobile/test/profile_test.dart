import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/core/router.dart';
import 'package:myradius/features/feed/feed_models.dart';
import 'package:myradius/features/profile/profile_models.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

Map<String, dynamic> kamau({bool postsVisible = true}) => {
      'id': 'u2',
      'display_name': 'Kamau N.',
      'is_verified_neighbour': true,
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'distance_bucket': '1_3km',
      'member_since': '2026-03-02T09:00:00Z',
      'rating_average': 4.8,
      'rating_count': 12,
      'bio': 'Plumber, 10 years. Tanks and leaks.',
      'avatar': {
        '320': {'url': 'http://storage.test/a320.webp'},
      },
      'posts_visible': postsVisible,
    };

void main() {
  group('models', () {
    test('photo sizes come as a map by width, and the smallest that fits is used', () {
      final sizes = imageSizes({
        '320': {'url': 'a'},
        '720': {'url': 'b'},
        '1080': {'url': 'c'},
      });
      expect(sizeFor(sizes, 300), 'a');
      expect(sizeFor(sizes, 700), 'b');
      expect(sizeFor(sizes, 4000), 'c');
      expect(sizeFor(const {}, 100), isNull);
    });

    test('a profile reads its photo, bio and where, never a position', () {
      final profile = Profile.fromJson(kamau());
      expect(profile.avatar[320], 'http://storage.test/a320.webp');
      expect(profile.bio, startsWith('Plumber'));
      expect(profile.where, 'Nyeri Town · 1–3 km');
    });
  });

  group('screens', () {
    late FakeAdapter adapter;
    late Map<String, dynamic> profile;
    late Map<String, dynamic> me;

    FakeReply handle(RequestOptions request) {
      final path = request.path;
      if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
      if (path == '/me') return FakeReply(200, me);
      if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
      if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
      if (path == '/users/u2') return FakeReply(200, profile);
      if (path == '/users/u2/posts') return const FakeReply(200, {'items': [], 'next_cursor': null});
      if (path == '/users/u2/services') return const FakeReply(200, {'items': [], 'next_cursor': null});
      if (path == '/users/u2/listings') {
        return const FakeReply(200, {
          'items': [
            {
              'id': 'l1',
              'seller': {'id': 'u2', 'display_name': 'Kamau N.', 'is_verified_neighbour': true},
              'title': 'Pipe wrench set',
              'description': '',
              'category': 'household',
              'condition': 'good',
              'pricing': 'fixed',
              'price_kes': 2500,
              'images': <Object>[],
              'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
              'availability': 'available',
              'is_expired': false,
              'expires_at': '2026-11-20T09:00:00Z',
              'is_mine': false,
              'saved': false,
              'status': 'visible',
              'created_at': '2026-10-21T09:00:00Z',
            },
          ],
          'next_cursor': null,
        });
      }
      if (path == '/conversations/direct') {
        return const FakeReply(200, {
          'id': 'c3',
          'kind': 'direct',
          'state': 'active',
          'my_role': 'member',
          'my_status': 'active',
          'muted': false,
          'other_user': {'id': 'u2', 'display_name': 'Kamau N.', 'is_verified_neighbour': true},
          'member_count': 2,
          'last_seq': 0,
          'last_read_seq': 0,
          'unread_count': 0,
          'created_at': '2026-10-21T09:00:00Z',
          'updated_at': '2026-10-21T09:00:00Z',
        });
      }
      return FakeReply(404, errorBody('not_found', 'Resource not found'));
    }

    Future<void> openKamau(WidgetTester tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      adapter = FakeAdapter(handle);
      final store = MemoryTokenStore()..refreshToken = 'r1';
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      final container = ProviderContainer(overrides: [
        tokenStoreProvider.overrideWithValue(store),
        apiClientProvider.overrideWithValue(api),
        realtimeProvider.overrideWith((ref) {
          final client = RealtimeClient(api, connect: FakeSockets().connect, uri: Uri.parse('ws://api.test/v1/ws'));
          ref.onDispose(client.dispose);
          return client;
        }),
      ]);
      addTearDown(container.dispose);
      await tester.pumpWidget(UncontrolledProviderScope(container: container, child: const MyRadiusApp()));
      await tester.pumpAndSettle();
      container.read(routerProvider).push(Routes.person('u2'));
      await tester.pumpAndSettle();
    }

    setUp(() => me = Map.of(meJson));

    testWidgets("someone else's profile: details, what they sell, and Message", (tester) async {
      profile = kamau();
      await openKamau(tester);
      expect(find.text('Kamau N.'), findsOneWidget);
      expect(find.text('Plumber, 10 years. Tanks and leaks.'), findsOneWidget);
      expect(find.text('Nyeri Town · 1–3 km · ★ 4.8 (12) · Joined Mar 2026'), findsOneWidget);
      expect(find.text('Edit profile'), findsNothing);
      expect(find.text('Kamau hasn\'t posted yet.'), findsOneWidget);

      await tester.tap(find.text('Selling'));
      await tester.pumpAndSettle();
      expect(find.text('Pipe wrench set'), findsOneWidget);

      await tester.tap(find.text('Message Kamau'));
      await tester.pumpAndSettle();
      final sent = adapter.requests.lastWhere((r) => r.path == '/conversations/direct');
      expect(sent.data, {'user_id': 'u2'});
    });

    testWidgets('from further away, posts are not listed, and the profile says why', (tester) async {
      profile = kamau(postsVisible: false);
      await openKamau(tester);
      expect(find.text("Kamau's posts are shown to neighbours nearby. You live further away."), findsOneWidget);
      expect(adapter.requests.where((r) => r.path == '/users/u2/posts'), isNotEmpty);
    });

    testWidgets('a newcomer is told why before a first message, and no chat opens', (tester) async {
      profile = kamau();
      me = {...meJson, 'can_contact_strangers': false, 'presence_days_this_week': 2, 'contact_unlock_days': 4};
      await openKamau(tester);

      await tester.tap(find.text('Message Kamau'));
      await tester.pumpAndSettle();
      expect(find.text('Settle in first'), findsOneWidget);
      expect(find.textContaining('So far: 2 of 4.'), findsOneWidget);

      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      // Still on the profile, no message sent.
      expect(find.text('Message Kamau'), findsOneWidget);
      expect(adapter.requests.where((r) => r.path.endsWith('/messages') && r.method == 'POST'), isEmpty);
    });
  });
}
