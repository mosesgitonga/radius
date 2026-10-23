import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/me/account_models.dart';
import 'package:myradius/features/me/me_screen.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> meJson({String? deletionRequestedAt, bool verified = false}) => {
      'id': 'u1',
      'display_name': 'Amina K.',
      'phone_number': '+254712345678',
      'status': deletionRequestedAt == null ? 'active' : 'deletion_pending',
      'member_since': '2026-10-16T09:00:00Z',
      'has_home_location': true,
      'is_verified_neighbour': verified,
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'rating_average': 4.5,
      'rating_count': 2,
      'deletion_requested_at': deletionRequestedAt,
    };

const standingJson = {
  'status': 'active',
  'suspended_until': null,
  'restricted': true,
  'restricted_until': '2026-11-01T00:00:00Z',
  'restriction_reason': 'Several posts were reported as spam.',
  'is_verified_neighbour': false,
  'vouches_received': 1,
  'warnings': [
    {'id': 'w1', 'reason': 'Please keep the marketplace for items you own.', 'created_at': '2026-10-20T09:00:00Z'},
  ],
};

/// Scrolls Settings (not the hidden tabs beside it) until [finder] is on screen.
Future<void> reachInSettings(WidgetTester tester, Finder finder) async {
  final inSettings = find.descendant(of: find.byType(AccountScreen), matching: finder);
  await tester.scrollUntilVisible(inSettings, 200,
      scrollable: find.descendant(of: find.byType(AccountScreen), matching: find.byType(Scrollable)).first);
  await tester.ensureVisible(inSettings);
  await tester.pumpAndSettle();
}

Map<String, dynamic> profileJson(Map<String, dynamic> me) => {
      'id': me['id'],
      'display_name': me['display_name'],
      'is_verified_neighbour': me['is_verified_neighbour'],
      'area_label': me['area_label'],
      'member_since': me['member_since'],
      'rating_average': me['rating_average'],
      'rating_count': me['rating_count'],
      'bio': me['bio'],
      'posts_visible': true,
    };

const postJson = {
  'id': 'p1',
  'author': {'id': 'u1', 'display_name': 'Amina K.', 'is_verified_neighbour': false},
  'body': 'Matatu stage has moved across the road',
  'category': 'alert',
  'images': <Object>[],
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
  'distance_bucket': null,
  'comment_count': 0,
  'reaction_count': 0,
  'my_reaction': null,
  'status': 'published',
  'created_at': '2026-10-20T08:00:00Z',
};

void main() {
  group('models', () {
    test('only marketing can be switched; the rest go with the account', () {
      final marketing = Consent.fromJson({
        'consent_type': 'marketing',
        'granted': false,
        'document_version': '1',
        'recorded_at': '2026-10-16T09:00:00Z',
      });
      final terms = Consent.fromJson({
        'consent_type': 'terms',
        'granted': true,
        'document_version': '1',
        'recorded_at': '2026-10-16T09:00:00Z',
      });
      expect(marketing.optional, isTrue);
      expect(terms.optional, isFalse);
    });

    test('a notification kind the app has not heard of still has a name', () {
      expect(notificationName('message'), 'Messages');
      expect(notificationName('group_poll_closed'), 'Group poll closed');
    });

    test('standing reads restrictions and warnings', () {
      final standing = Standing.fromJson(Map<String, dynamic>.from(standingJson));
      expect(standing.restricted, isTrue);
      expect(standing.suspended, isFalse);
      expect(standing.warnings.single.reason, startsWith('Please keep'));
    });
  });

  group('screens', () {
    late FakeAdapter adapter;
    late MemoryTokenStore store;
    late Map<String, dynamic> me;
    late FakeReply Function(RequestOptions) extra;

    FakeReply handle(RequestOptions request) {
      final custom = extra(request);
      if (custom.status != 0) return custom;
      final path = request.path;
      if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
      if (path == '/me' && request.method == 'GET') return FakeReply(200, me);
      if (path == '/me' && request.method == 'PATCH') {
        final body = request.data as Map;
        me = {...me, 'display_name': body['display_name'], 'bio': body['bio']};
        return FakeReply(200, me);
      }
      if (path == '/users/u1') return FakeReply(200, profileJson(me));
      if (path == '/users/u1/posts') return const FakeReply(200, {'items': [postJson], 'next_cursor': null});
      if (path == '/users/u1/listings' || path == '/users/u1/services') {
        return const FakeReply(200, {'items': [], 'next_cursor': null});
      }
      if (path == '/me/standing') return const FakeReply(200, standingJson);
      if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
      if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
      if (path == '/me/deletion-request' && request.method == 'POST') {
        return const FakeReply(202, {'requested_at': '2026-10-23T09:00:00Z', 'purge_after': '2026-11-22T09:00:00Z'});
      }
      if (path == '/me/deletion-request' && request.method == 'DELETE') {
        me = meJson();
        return const FakeReply(204);
      }
      if (path == '/auth/sessions') {
        return const FakeReply(200, [
          {
            'id': 's1',
            'device_name': null,
            'device_platform': 'android',
            'created_at': '2026-10-16T09:00:00Z',
            'last_used_at': '2026-10-23T09:00:00Z',
            'expires_at': '2026-11-16T09:00:00Z',
            'is_current': true,
          },
          {
            'id': 's2',
            'device_name': null,
            'device_platform': 'other',
            'created_at': '2026-10-17T09:00:00Z',
            'last_used_at': '2026-10-17T09:00:00Z',
            'expires_at': '2026-11-17T09:00:00Z',
            'is_current': false,
          },
        ]);
      }
      if (path == '/auth/logout-all') return const FakeReply(200, {'sessions_revoked': 1});
      return FakeReply(404, errorBody('not_found', 'Resource not found'));
    }

    Future<void> openMe(WidgetTester tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      adapter = FakeAdapter(handle);
      store = MemoryTokenStore()..refreshToken = 'r1';
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
      await tester.tap(find.text('Me'));
      await tester.pumpAndSettle();
    }

    Future<void> openSettings(WidgetTester tester) async {
      await openMe(tester);
      await tester.tap(find.byTooltip('Settings'));
      await tester.pumpAndSettle();
    }

    setUp(() {
      me = meJson();
      extra = (_) => const FakeReply(0);
    });

    testWidgets('the Me tab is my profile, with my posts, and I can edit it', (tester) async {
      await openMe(tester);
      expect(find.text('Amina K.'), findsWidgets);
      expect(find.text('Nyeri Town · ★ 4.5 (2) · Joined Oct 2026'), findsOneWidget);
      expect(find.text('Matatu stage has moved across the road'), findsOneWidget);

      await tester.tap(find.text('Selling'));
      await tester.pumpAndSettle();
      expect(find.text('Nothing for sale. Sell something from the Market.'), findsOneWidget);

      await tester.tap(find.text('Edit profile'));
      await tester.pumpAndSettle();
      await tester.enterText(find.widgetWithText(TextField, 'Name'), 'Amina Kariuki');
      await tester.enterText(find.widgetWithText(TextField, 'Bio'), 'Tailor in Kamakwa. School uniforms.');
      final save = find.widgetWithText(FilledButton, 'Save');
      await tester.scrollUntilVisible(save, 200, scrollable: find.byType(Scrollable).last);
      await tester.tap(save);
      await tester.pumpAndSettle();

      final sent = adapter.requests.lastWhere((r) => r.path == '/me' && r.method == 'PATCH').data as Map;
      expect(sent, {'display_name': 'Amina Kariuki', 'bio': 'Tailor in Kamakwa. School uniforms.'});
      // Back on the profile, reloaded.
      expect(find.text('Amina Kariuki'), findsWidgets);
      expect(find.text('Tailor in Kamakwa. School uniforms.'), findsOneWidget);
    });

    testWidgets('settings show where the account stands', (tester) async {
      await openSettings(tester);
      expect(find.text('Restricted'), findsOneWidget);
      expect(find.textContaining('Several posts were reported as spam.'), findsOneWidget);
      expect(find.textContaining('1 neighbour has vouched for you'), findsOneWidget);
    });

    testWidgets('deleting asks for understanding, then signs out here', (tester) async {
      await openSettings(tester);
      await reachInSettings(tester, find.text('Delete my account'));
      await tester.tap(find.text('Delete my account'));
      await tester.pumpAndSettle();

      final delete = find.widgetWithText(FilledButton, 'Delete my account');
      await tester.scrollUntilVisible(delete, 200, scrollable: find.byType(Scrollable).last);
      await tester.pumpAndSettle();
      expect(tester.widget<FilledButton>(delete).onPressed, isNull);
      await tester.tap(find.text('I understand my account will be deleted after 30 days'));
      await tester.pump();
      await tester.tap(delete);
      await tester.pumpAndSettle();
      expect(find.textContaining('22 Nov 2026'), findsOneWidget);

      await tester.tap(find.text('OK'));
      await tester.pumpAndSettle();
      expect(store.refreshToken, isNull);
      expect(find.text("What's happening around you"), findsOneWidget);
    });

    testWidgets('signing in during the grace period offers to cancel', (tester) async {
      me = meJson(deletionRequestedAt: '2026-10-23T09:00:00Z');
      await openMe(tester);
      // Across the app, on every tab.
      expect(find.text('Your account is due to be deleted'), findsOneWidget);
      await tester.tap(find.byTooltip('Settings'));
      await tester.pumpAndSettle();
      // And in Settings, with the button.
      expect(find.text('Your account is due to be deleted'), findsNWidgets(2));
      expect(find.text('Delete my account'), findsNothing);

      await tester.tap(find.text('Cancel deletion'));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/me/deletion-request' && r.method == 'DELETE'), isTrue);
      expect(find.text('Your account is due to be deleted'), findsNothing);
    });

    testWidgets('signing out other devices keeps this one', (tester) async {
      await openSettings(tester);
      await reachInSettings(tester, find.text("Where I'm signed in"));
      await tester.tap(find.text("Where I'm signed in"));
      await tester.pumpAndSettle();
      expect(find.text('Android phone · this phone'), findsOneWidget);

      await tester.tap(find.text('Sign out everywhere else'));
      await tester.pumpAndSettle();
      final sent = adapter.requests.lastWhere((r) => r.path == '/auth/logout-all');
      expect(sent.data, {'keep_current_session': true});
      expect(store.refreshToken, isNotNull);
    });
  });
}
