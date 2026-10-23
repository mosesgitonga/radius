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

/// Scrolls the Me tab (not the hidden tabs beside it) until [finder] is on screen.
Future<void> reachInMe(WidgetTester tester, Finder finder) async {
  final inMe = find.descendant(of: find.byType(MeScreen), matching: finder);
  await tester.scrollUntilVisible(inMe, 200,
      scrollable: find.descendant(of: find.byType(MeScreen), matching: find.byType(Scrollable)).first);
  await tester.ensureVisible(inMe);
  await tester.pumpAndSettle();
}

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
        me = {...me, 'display_name': (request.data as Map)['display_name']};
        return FakeReply(200, me);
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

    setUp(() {
      me = meJson();
      extra = (_) => const FakeReply(0);
    });

    testWidgets('the account, its standing, and a new name', (tester) async {
      await openMe(tester);
      expect(find.text('Amina K.'), findsOneWidget);
      expect(find.text('Nyeri Town · ★ 4.5 (2)'), findsOneWidget);
      expect(find.text('Restricted'), findsOneWidget);
      expect(find.textContaining('Several posts were reported as spam.'), findsOneWidget);
      expect(find.textContaining('1 neighbour has vouched for you'), findsOneWidget);

      await tester.tap(find.text('Edit'));
      await tester.pumpAndSettle();
      await tester.enterText(find.byType(TextField), 'Amina Kariuki');
      await tester.tap(find.text('Save'));
      await tester.pumpAndSettle();
      expect(find.text('Amina Kariuki'), findsOneWidget);
    });

    testWidgets('deleting asks for understanding, then signs out here', (tester) async {
      await openMe(tester);
      await reachInMe(tester, find.text('Delete my account'));
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
      // Once across the app, once on the Me tab with the button.
      expect(find.text('Your account is due to be deleted'), findsNWidgets(2));
      expect(find.text('Delete my account'), findsNothing);

      await tester.tap(find.text('Cancel deletion'));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/me/deletion-request' && r.method == 'DELETE'), isTrue);
      expect(find.text('Your account is due to be deleted'), findsNothing);
    });

    testWidgets('signing out other devices keeps this one', (tester) async {
      await openMe(tester);
      await reachInMe(tester, find.text("Where I'm signed in"));
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
