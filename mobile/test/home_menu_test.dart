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
import 'support/fake_socket.dart';

Map<String, dynamic> me({bool proven = true, bool listed = false, int daysSoFar = 2}) => {
      'id': 'u1',
      'display_name': 'Amina K.',
      'status': 'active',
      'has_home_location': true,
      'is_verified_neighbour': false,
      'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
      'can_contact_strangers': proven,
      'presence_days_this_week': daysSoFar,
      'contact_unlock_days': 4,
      'contact_unlock_window_days': 7,
      'show_in_nearby': listed,
    };

const wanjiku = {
  'id': 'u2',
  'display_name': 'Wanjiku M.',
  'is_verified_neighbour': true,
  'area_label': {'label': 'Kamakwa', 'precision': 'neighbourhood'},
  'distance_bucket': '1_3km',
  'bio': 'Tailor. Alterations while you wait.',
  'member_since': '2026-09-01T08:00:00Z',
};

void main() {
  late FakeAdapter adapter;
  late Map<String, dynamic> account;
  late List<Map<String, dynamic>> listed;

  FakeReply handle(RequestOptions request) {
    final path = request.path;
    if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
    if (path == '/me' && request.method == 'PATCH') {
      account = {...account, ...Map<String, dynamic>.from(request.data as Map)};
      return FakeReply(200, account);
    }
    if (path == '/me') return FakeReply(200, account);
    if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 3});
    if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
    if (path == '/people/nearby') return FakeReply(200, {'items': listed, 'next_cursor': null});
    if (path == '/me/consents') return const FakeReply(200, []);
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

  Future<void> openMenu(WidgetTester tester) async {
    await tester.tap(find.byTooltip('Menu'));
    await tester.pumpAndSettle();
  }

  setUp(() {
    account = me();
    listed = [wanjiku];
  });

  testWidgets('the menu shows the town, Services & jobs and People nearby, and no friends',
      (tester) async {
    await openApp(tester);
    await openMenu(tester);

    final drawer = find.byType(Drawer);
    expect(find.descendant(of: drawer, matching: find.text('Nyeri Town')), findsOneWidget);
    expect(find.descendant(of: drawer, matching: find.text('Your feed reaches 3 km from home')), findsOneWidget);
    expect(find.descendant(of: drawer, matching: find.text('Services & jobs')), findsOneWidget);
    expect(find.descendant(of: drawer, matching: find.text('People nearby')), findsOneWidget);
    expect(find.textContaining('Friend'), findsNothing);
  });

  testWidgets('People nearby lists neighbours and offers to list me', (tester) async {
    await openApp(tester);
    await openMenu(tester);
    await tester.tap(find.text('People nearby'));
    await tester.pumpAndSettle();

    expect(find.text('Wanjiku M.'), findsOneWidget);
    expect(find.text('Kamakwa · 1–3 km'), findsOneWidget);
    expect(find.text("You're not listed here"), findsOneWidget);

    await tester.tap(find.text('Show me here'));
    await tester.pumpAndSettle();
    final patch = adapter.requests.lastWhere((r) => r.method == 'PATCH');
    expect(patch.data, {'show_in_nearby': true});
    expect(find.text("You're not listed here"), findsNothing);
  });

  testWidgets('People nearby waits until my home is proven, and asks nothing of the server',
      (tester) async {
    account = me(proven: false, daysSoFar: 2);
    await openApp(tester);
    await openMenu(tester);
    await tester.tap(find.text('People nearby'));
    await tester.pumpAndSettle();

    expect(find.text('Settle in first'), findsOneWidget);
    expect(find.textContaining('4 of the last 7 days. So far: 2 of 4.'), findsOneWidget);
    expect(adapter.requests.where((r) => r.path == '/people/nearby'), isEmpty);
  });

  testWidgets('an empty list says so', (tester) async {
    account = me(listed: true);
    listed = [];
    await openApp(tester);
    await openMenu(tester);
    await tester.tap(find.text('People nearby'));
    await tester.pumpAndSettle();
    expect(find.text('Nobody nearby is listed yet'), findsOneWidget);
    expect(find.text("You're not listed here"), findsNothing);
  });
}
