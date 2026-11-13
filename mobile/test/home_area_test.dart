import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_location.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> me({bool hasHome = false, bool provisional = false, String? confirmBy}) => {
      'id': 'u1',
      'display_name': 'Amina K.',
      'status': 'active',
      'has_home_location': hasHome,
      'is_verified_neighbour': false,
      'area_label': hasHome ? {'label': 'Rware', 'precision': 'area'} : null,
      'home_provisional': provisional,
      'home_confirm_by': confirmBy,
    };

void main() {
  late FakeAdapter adapter;
  late Map<String, dynamic> account;

  FakeReply handle(RequestOptions request) {
    final path = request.path;
    if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
    if (path == '/me') return FakeReply(200, account);
    if (path == '/me/location' && request.method == 'PUT') {
      final away = (request.data as Map)['at_home'] == false;
      account = me(hasHome: true, provisional: away, confirmBy: away ? DateTime.now().toUtc().add(const Duration(days: 5)).toIso8601String() : null);
      return const FakeReply(200, {
        'area_label': {'label': 'Rware', 'precision': 'area'},
        'is_first_time': true,
      });
    }
    if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
    if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
    return FakeReply(404, errorBody('not_found', 'Resource not found'));
  }

  Future<void> openApp(WidgetTester tester) async {
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
        locationReaderProvider.overrideWithValue(const FakeReader()),
        realtimeProvider.overrideWith((ref) {
          final client = RealtimeClient(api, connect: FakeSockets().connect, uri: Uri.parse('ws://api.test/v1/ws'));
          ref.onDispose(client.dispose);
          return client;
        }),
      ],
      child: const MyRadiusApp(),
    ));
    await tester.pumpAndSettle();
  }

  testWidgets('away from home: a provisional home, then a reminder on Home', (tester) async {
    account = me();
    await openApp(tester);
    expect(find.text('Are you at home right now?'), findsOneWidget);

    await tester.tap(find.text("No, I'm away from home"));
    await tester.pumpAndSettle();
    final put = adapter.requests.singleWhere((r) => r.path == '/me/location');
    expect((put.data as Map)['at_home'], isFalse);
    expect(find.text("You're in Rware for now"), findsOneWidget);

    await tester.tap(find.text('Continue'));
    await tester.pumpAndSettle();
    expect(find.text("You're using MyRadius from Rware for now"), findsOneWidget);
    expect(find.text("I'm at home now"), findsOneWidget);
  });

  testWidgets('at home: the home is set as it always was', (tester) async {
    account = me();
    await openApp(tester);
    await tester.tap(find.text("Yes, I'm at home"));
    await tester.pumpAndSettle();
    final put = adapter.requests.singleWhere((r) => r.path == '/me/location');
    expect((put.data as Map)['at_home'], isTrue);
    expect(find.text('Welcome to Rware'), findsOneWidget);

    await tester.tap(find.text('This is home'));
    await tester.pumpAndSettle();
    expect(find.textContaining('for now'), findsNothing);
  });
}
