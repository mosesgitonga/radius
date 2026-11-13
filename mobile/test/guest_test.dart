import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_location.dart';

Map<String, dynamic> guestPost(String id, String body) => {
      'id': id,
      'body': body,
      'category': 'alert',
      'images': [],
      'area_label': {'label': 'Kamakwa', 'precision': 'neighbourhood'},
      'distance_bucket': 'under_1km',
      'comment_count': 3,
      'reaction_count': 5,
      'created_at': '2026-11-03T08:00:00Z',
    };

void main() {
  late FakeAdapter adapter;

  FakeReply handle(RequestOptions request) {
    if (request.path == '/guest/feed') {
      return FakeReply(200, {
        'items': [guestPost('p1', 'Water is off on Saturday from 9am')],
        'next_cursor': null,
        'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
        'radius_km': 15,
      });
    }
    return FakeReply(404, errorBody('not_found', 'Resource not found'));
  }

  Future<void> openApp(WidgetTester tester, {String? problem}) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 2.75;
    addTearDown(tester.view.reset);
    adapter = FakeAdapter(handle);
    final store = MemoryTokenStore();
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    await tester.pumpWidget(ProviderScope(
      overrides: [
        tokenStoreProvider.overrideWithValue(store),
        apiClientProvider.overrideWithValue(api),
        locationReaderProvider.overrideWithValue(FakeReader(problem: problem)),
      ],
      child: const MyRadiusApp(),
    ));
    await tester.pumpAndSettle();
  }

  testWidgets('a first open shows the feed around the phone, with no names', (tester) async {
    await openApp(tester);

    expect(find.text('Nyeri Town'), findsOneWidget);
    expect(find.text('Water is off on Saturday from 9am'), findsOneWidget);
    expect(find.text('A neighbour'), findsOneWidget);
    final request = adapter.requests.singleWhere((r) => r.path == '/guest/feed');
    expect(request.method, 'POST');
    expect(request.data, {'latitude': -0.4201, 'longitude': 36.9476, 'accuracy_metres': 12.0, 'is_mocked': false});
    expect(request.queryParameters, isEmpty);
  });

  for (final (what, prompt) in [
    ('5 helpful', 'Join Hoodora to mark posts helpful'),
    ('New post', 'Join Hoodora to post'),
    ('Market', 'Join Hoodora to buy and sell'),
    ('Chats', 'Join Hoodora to chat with neighbours'),
    ('Me', 'Join Hoodora to have a profile'),
  ]) {
    testWidgets('$what asks to join', (tester) async {
      await openApp(tester);
      final target = what == 'New post' ? find.byTooltip('New post') : find.text(what);
      await tester.tap(target.first);
      await tester.pumpAndSettle();
      expect(find.text(prompt), findsOneWidget);
      await tester.tap(find.text('Not now'));
      await tester.pumpAndSettle();
      expect(find.text(prompt), findsNothing);
    });
  }

  testWidgets('opening a post or its author asks to join, and Sign up goes to the phone screen',
      (tester) async {
    await openApp(tester);
    await tester.tap(find.text('Water is off on Saturday from 9am'));
    await tester.pumpAndSettle();
    expect(find.text('Join Hoodora to read the comments'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, 'Sign up').last);
    await tester.pumpAndSettle();
    expect(find.text("What's happening around you"), findsOneWidget);
  });

  testWidgets('refusing location explains and offers sign-up', (tester) async {
    await openApp(tester, problem: 'Hoodora needs your location to find your area.');
    expect(find.text('Hoodora needs your location to find your area.'), findsOneWidget);
    expect(adapter.requests.where((r) => r.path == '/guest/feed'), isEmpty);
    await tester.tap(find.text('Sign up instead'));
    await tester.pumpAndSettle();
    expect(find.text("What's happening around you"), findsOneWidget);
  });
}
