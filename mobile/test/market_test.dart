import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/market/listing_models.dart';
import 'package:myradius/features/market/screens/sell_screen.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> listingJson(
  String id, {
  String title = '5-seater sofa set, grey',
  String pricing = 'negotiable',
  int price = 18000,
  String availability = 'available',
  bool saved = false,
}) =>
    {
      'id': id,
      'seller': {'id': 'u2', 'display_name': 'Wanjiku M.', 'is_verified_neighbour': true},
      'title': title,
      'description': 'Two years old, covers come off for washing.',
      'category': 'furniture',
      'condition': 'good',
      'pricing': pricing,
      'price_kes': price,
      'images': <Object>[],
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'distance_bucket': 'under_1km',
      'availability': availability,
      'is_expired': false,
      'expires_at': '2026-11-20T09:00:00Z',
      'is_mine': false,
      'saved': saved,
      'status': 'visible',
      'created_at': '2026-10-21T09:00:00Z',
    };

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

Map<String, dynamic> conversationJson() => {
      'id': 'c7',
      'kind': 'listing',
      'listing_id': 'l1',
      'state': 'active',
      'my_role': 'member',
      'my_status': 'active',
      'muted': false,
      'other_user': {'id': 'u2', 'display_name': 'Wanjiku M.', 'is_verified_neighbour': true},
      'member_count': 2,
      'last_seq': 0,
      'last_read_seq': 0,
      'unread_count': 0,
      'created_at': '2026-10-21T09:00:00Z',
      'updated_at': '2026-10-21T09:00:00Z',
    };

void main() {
  group('models', () {
    test('a free item says so, whatever its price field', () {
      expect(priceLabel('free', 0), 'Free');
      expect(priceLabel('negotiable', 18000), 'KES 18,000');
    });

    test('categories have names, even ones the app has not heard of', () {
      expect(listingCategoryLabel('clothing_and_shoes'), 'Clothes & shoes');
      expect(listingCategoryLabel('solar_panels'), 'Solar panels');
    });

    test('only an available or reserved, unexpired listing can still be asked about', () {
      expect(Listing.fromJson(listingJson('a')).isOpen, isTrue);
      expect(Listing.fromJson(listingJson('b', availability: 'reserved')).isOpen, isTrue);
      expect(Listing.fromJson(listingJson('c', availability: 'sold')).isOpen, isFalse);
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
      if (path == '/listings' && request.method == 'GET') {
        return FakeReply(200, {
          'items': [listingJson('l1'), listingJson('l2', title: 'Moving boxes', pricing: 'free', price: 0)],
          'next_cursor': null,
          'radius_km': 30,
          'sort': 'newest',
        });
      }
      if (path == '/listings/l1') return FakeReply(200, listingJson('l1'));
      if (path == '/listings/l1/save') return const FakeReply(204);
      if (path == '/users/u2/reviews') {
        return const FakeReply(200, {'items': [], 'next_cursor': null, 'rating_average': 4.5, 'rating_count': 2});
      }
      if (path == '/listings/l1/chat') return FakeReply(200, conversationJson());
      if (path == '/conversations/c7') return FakeReply(200, conversationJson());
      if (path == '/conversations/c7/messages') return const FakeReply(200, {'items': [], 'has_more': false});
      return FakeReply(404, errorBody('not_found', 'Resource not found'));
    }

    Future<void> openMarket(WidgetTester tester) async {
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
      await tester.tap(find.text('Market'));
      await tester.pumpAndSettle();
    }

    setUp(() => extra = (_) => const FakeReply(0));

    testWidgets('browse, save, and ask the seller, with the listing at the top of the chat', (tester) async {
      await openMarket(tester);
      expect(find.text('KES 18,000'), findsOneWidget);
      expect(find.text('Free'), findsWidgets);
      expect(find.text('Within 30 km of home'), findsOneWidget);

      await tester.tap(find.text('5-seater sofa set, grey'));
      await tester.pumpAndSettle();
      expect(find.text('★ 4.5 from 2 reviews'), findsOneWidget);
      expect(find.text('Good condition'), findsOneWidget);
      expect(find.text('Negotiable'), findsOneWidget);

      await tester.tap(find.byTooltip('Save'));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/listings/l1/save' && r.method == 'PUT'), isTrue);
      expect(find.byTooltip('Unsave'), findsOneWidget);

      await tester.tap(find.text("Ask if it's available"));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/listings/l1/chat' && r.method == 'POST'), isTrue);
      expect(find.text('5-seater sofa set, grey'), findsOneWidget);
      expect(find.text('KES 18,000 · Available'), findsOneWidget);
    });

    testWidgets('selling: a prohibited item is refused naming the word', (tester) async {
      extra = (request) => request.path == '/listings' && request.method == 'POST'
          ? FakeReply(400, {
              'error': {
                'code': 'bad_request',
                'message': 'This item cannot be listed on Hoodora',
                'details': {'reason': 'prohibited_item', 'term': 'rifle'},
              },
            })
          : const FakeReply(0);
      await openMarket(tester);
      await tester.tap(find.text('Sell'));
      await tester.pumpAndSettle();

      Finder inForm(Finder finder) => find.descendant(of: find.byType(SellScreen), matching: finder);
      final scrollable = inForm(find.byType(Scrollable)).first;
      Future<Finder> reach(Finder finder) async {
        await tester.scrollUntilVisible(inForm(finder), 150, scrollable: scrollable);
        await tester.ensureVisible(inForm(finder));
        await tester.pumpAndSettle();
        return inForm(finder);
      }

      await tester.enterText(await reach(find.widgetWithText(TextField, 'What are you selling?')), 'Air rifle');
      await tester.tap(await reach(find.text('Sports')));
      await tester.enterText(await reach(find.widgetWithText(TextField, 'Price')), '5000');
      await tester.pump();
      await tester.tap(await reach(find.widgetWithText(FilledButton, 'List it')));
      await tester.pumpAndSettle();

      final sent = adapter.requests.lastWhere((r) => r.path == '/listings' && r.method == 'POST').data as Map;
      expect(sent['category'], 'sports');
      expect(sent['condition'], 'good');
      expect(sent['pricing'], 'fixed');
      expect(sent['price_kes'], 5000);
      expect(sent['media_ids'], isEmpty);
      expect(find.textContaining('it mentions "rifle"'), findsOneWidget);
    });
  });
}
