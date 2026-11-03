import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/feed/feed_models.dart';
import 'package:myradius/features/services/screens/service_compose_screen.dart';
import 'package:myradius/features/services/service_models.dart';
import 'package:myradius/features/services/services_controller.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> servicePost(
  String id, {
  String kind = 'offering',
  String title = 'Plumber for tanks and leaks',
  String authorId = 'u2',
  String? conversationId,
}) =>
    {
      'id': id,
      'author': {
        'id': authorId,
        'display_name': 'Kamau N.',
        'is_verified_neighbour': true,
        'rating_average': 4.8,
        'rating_count': 12,
      },
      'kind': kind,
      'category': 'plumbing',
      'title': title,
      'description': 'Tanks, leaks and new fittings.',
      'rate_info': kind == 'offering' ? 'KES 800 per visit' : null,
      'travel_km': kind == 'offering' ? 10 : null,
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'distance_bucket': 'under_1km',
      'is_open': true,
      'is_mine': authorId == 'u1',
      'my_conversation_id': conversationId,
      'safety_notice': kind == 'hiring' ? 'Never pay to get a job.' : null,
      'status': 'visible',
      'created_at': '2026-10-17T08:00:00Z',
    };

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

Map<String, dynamic> conversation(String id) => {
      'id': id,
      'kind': 'service',
      'service_post_id': 's1',
      'state': 'active',
      'my_role': 'member',
      'my_status': 'active',
      'muted': false,
      'other_user': {'id': 'u2', 'display_name': 'Kamau N.', 'is_verified_neighbour': true},
      'member_count': 2,
      'last_seq': 0,
      'last_read_seq': 0,
      'unread_count': 0,
      'created_at': '2026-10-17T09:00:00Z',
      'updated_at': '2026-10-17T09:00:00Z',
    };

void main() {
  group('models', () {
    test('pay reads as a range, a floor, or one figure', () {
      expect(payLabel(15000, 18000, 'month'), 'KES 15,000–18,000 / month');
      expect(payLabel(1000, null, 'day'), 'From KES 1,000 / day');
      expect(payLabel(null, 500, 'hour'), 'Up to KES 500 / hour');
      expect(payLabel(3000, 3000, 'one_off'), 'KES 3,000, one-off');
      expect(payLabel(null, null, 'month'), isNull);
    });

    test('categories have names, even ones the app has not heard of', () {
      expect(categoryLabel('salon_barber'), 'Salon & barber');
      expect(categoryLabel('solar_install'), 'Solar install');
    });

    test('a provider with no reviews is new', () {
      expect(ratingLabel(null, 0), 'New');
      expect(ratingLabel(4.84, 12), '★ 4.8 (12)');
    });

    test('initials come from letters and digits only', () {
      const author = Author(id: 'x', displayName: 'Wanjiru (smoke 6)', isVerifiedNeighbour: false);
      expect(author.initials, 'WS');
    });
  });

  group('browse', () {
    test('kind, category and search go to the server, and a stale answer is dropped', () async {
      final adapter = FakeAdapter((request) => FakeReply(200, {
            'items': [servicePost('s-${request.queryParameters['kind']}-${request.queryParameters['q'] ?? ''}')],
            'next_cursor': null,
            'radius_km': 40,
            'sort': 'newest',
          }));
      final api = ApiClient(store: MemoryTokenStore(), dio: fakeDio(adapter));
      await api.startSession(const TokenPair(accessToken: 'a1', refreshToken: 'r1'));
      final container = ProviderContainer(overrides: [apiClientProvider.overrideWithValue(api)]);
      addTearDown(container.dispose);
      container.listen(servicesControllerProvider, (_, _) {});
      final controller = container.read(servicesControllerProvider.notifier);

      controller.setKind(ServiceKind.hiring);
      controller.setCategory('plumbing');
      controller.setQuery('attendant');
      for (var i = 0; i < 100 && container.read(servicesControllerProvider).loading; i++) {
        await Future<void>.delayed(const Duration(milliseconds: 1));
      }
      final last = adapter.requests.last.queryParameters;
      expect(last, {'kind': 'hiring', 'category': 'plumbing', 'q': 'attendant'});
      // Only the newest search's answer is shown.
      expect(container.read(servicesControllerProvider).posts.single.id, 's-hiring-attendant');
    });
  });

  group('screens', () {
    late FakeAdapter adapter;
    late FakeReply Function(RequestOptions) extra;

    FakeReply handle(RequestOptions request) {
      final custom = extra(request);
      if (custom.status != 0) return custom;
      final path = request.path;
      if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
      if (path == '/me') return const FakeReply(200, meJson);
      if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
      if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
      if (path == '/services' && request.method == 'GET') {
        return FakeReply(200, {
          'items': [servicePost('s1')],
          'next_cursor': null,
          'radius_km': 40,
          'sort': 'newest',
        });
      }
      if (path == '/services/s1') return FakeReply(200, servicePost('s1'));
      if (path == '/users/u2/reviews') {
        return const FakeReply(200, {
          'items': [
            {
              'id': 'r1',
              'reviewer': {'id': 'u3', 'display_name': 'Peter M.', 'is_verified_neighbour': false},
              'subject_id': 'u2',
              'context_type': 'service_post',
              'context_id': 's1',
              'rating': 4,
              'body': 'Good work on the kitchen sink.',
              'status': 'visible',
              'created_at': '2026-10-10T09:00:00Z',
              'updated_at': '2026-10-10T09:00:00Z',
            },
          ],
          'next_cursor': null,
          'rating_average': 4.8,
          'rating_count': 12,
        });
      }
      if (path == '/services/s1/chat') return FakeReply(200, conversation('c9'));
      if (path == '/conversations/c9') return FakeReply(200, conversation('c9'));
      if (path == '/conversations/c9/messages') return const FakeReply(200, {'items': [], 'has_more': false});
      return FakeReply(404, errorBody('not_found', 'Resource not found'));
    }

    Future<void> openServices(WidgetTester tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      adapter = FakeAdapter(handle);
      final store = MemoryTokenStore()..refreshToken = 'r1';
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
      await tester.tap(find.byTooltip('Menu'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Services & jobs'));
      await tester.pumpAndSettle();
    }

    setUp(() => extra = (_) => const FakeReply(0));

    testWidgets('find a provider, read their reviews, and message them about the post', (tester) async {
      await openServices(tester);
      expect(find.text('Kamau N.'), findsOneWidget);
      expect(find.text('★ 4.8 (12)'), findsOneWidget);
      expect(find.text('Travels up to 10 km'), findsOneWidget);

      await tester.tap(find.text('Kamau N.'));
      await tester.pumpAndSettle();
      expect(find.text('Good work on the kitchen sink.'), findsOneWidget);
      expect(find.text('12 reviews'), findsOneWidget);

      await tester.tap(find.text('Message Kamau'));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/services/s1/chat' && r.method == 'POST'), isTrue);
      // The conversation, with the post it is about at the top.
      expect(find.text('Plumber for tanks and leaks'), findsOneWidget);
      expect(find.text('Plumbing · KES 800 per visit'), findsOneWidget);
    });

    testWidgets('a job sends only job fields, and a new account is told when it may post', (tester) async {
      extra = (request) => request.path == '/services' && request.method == 'POST'
          ? FakeReply(403, {
              'error': {
                'code': 'forbidden',
                'message': 'New accounts cannot post jobs yet',
                'details': {'reason': 'hiring_trust_threshold', 'min_account_age_days': 14},
              },
            })
          : const FakeReply(0);
      await openServices(tester);
      await tester.tap(find.text('Jobs'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Post'));
      await tester.pumpAndSettle();
      expect(find.text("I'm hiring"), findsOneWidget);

      // The form is a lazy list over the Services screen, which has chips of its
      // own: look only inside the form, and scroll each field into view first.
      Finder inForm(Finder finder) => find.descendant(of: find.byType(ServiceComposeScreen), matching: finder);
      final scrollable = inForm(find.byType(Scrollable)).first;
      Future<Finder> reach(Finder finder) async {
        await tester.scrollUntilVisible(inForm(finder), 150, scrollable: scrollable);
        // Built is not on screen: a lazy list builds a little beyond its edge.
        await tester.ensureVisible(inForm(finder));
        await tester.pumpAndSettle();
        return inForm(finder);
      }

      await tester.tap(await reach(find.text('Other')));
      await tester.enterText(await reach(find.widgetWithText(TextField, 'Job title')), 'Shop attendant');
      await tester.tap(await reach(find.text('Full time')));
      await tester.enterText(await reach(find.widgetWithText(TextField, 'From')), '15000');
      await tester.enterText(await reach(find.widgetWithText(TextField, 'To')), '18000');
      await tester.tap(await reach(find.text('Per month')));
      await tester.pump();
      await tester.tap(await reach(find.widgetWithText(FilledButton, 'Post')));
      await tester.pumpAndSettle();

      final sent = adapter.requests.lastWhere((r) => r.path == '/services' && r.method == 'POST').data as Map;
      expect(sent['kind'], 'hiring');
      expect(sent['job_type'], 'full_time');
      expect(sent['pay_min_kes'], 15000);
      expect(sent['pay_max_kes'], 18000);
      expect(sent['pay_period'], 'month');
      expect(sent.containsKey('rate_info'), isFalse);
      expect(sent.containsKey('travel_km'), isFalse);
      expect(find.textContaining('Accounts can post jobs once they are 14 days old'), findsOneWidget);
    });
  });
}
