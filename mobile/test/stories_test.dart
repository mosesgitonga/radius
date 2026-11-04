import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/stories/story_models.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_location.dart';
import 'support/fake_socket.dart';

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Rware', 'precision': 'area'},
};

Map<String, dynamic> story(
  String id,
  String authorId,
  String name,
  String text, {
  bool seen = false,
  int? views,
  String created = '2026-11-04T08:00:00Z',
}) => {
  'id': id,
  'author': {'id': authorId, 'display_name': name, 'is_verified_neighbour': false},
  'kind': 'text',
  'text': text,
  'area_label': {'label': 'Rware', 'precision': 'area'},
  'distance_bucket': authorId == 'u1' ? null : 'under_1km',
  'expires_at': '2026-11-05T08:00:00Z',
  'created_at': created,
  'view_count': views,
  'seen': seen,
};

void main() {
  late FakeAdapter adapter;
  late List<Map<String, dynamic>> live;

  FakeReply handle(RequestOptions request) {
    final path = request.path;
    if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
    if (path == '/me') return const FakeReply(200, meJson);
    if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
    if (path == '/conversations') return const FakeReply(200, {'items': [], 'next_cursor': null});
    if (path == '/stories' && request.method == 'GET') return FakeReply(200, {'items': live, 'next_cursor': null});
    if (path == '/stories' && request.method == 'POST') {
      final body = Map<String, dynamic>.from(request.data as Map);
      return FakeReply(201, story('s-new', 'u1', 'Amina K.', body['text'] as String, seen: true, views: 0));
    }
    if (path.endsWith('/view')) return const FakeReply(204, null);
    if (path == '/stories/s-mine/viewers') {
      return const FakeReply(200, {
        'items': [
          {
            'user': {'id': 'u2', 'display_name': 'Wanjiku M.', 'is_verified_neighbour': false},
            'viewed_at': '2026-11-04T09:00:00Z',
          },
        ],
        'next_cursor': null,
      });
    }
    return FakeReply(404, errorBody('not_found', 'Resource not found'));
  }

  Future<void> openApp(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 2.75;
    addTearDown(tester.view.reset);
    adapter = FakeAdapter(handle);
    final store = MemoryTokenStore()..refreshToken = 'r1';
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    await tester.pumpWidget(
      ProviderScope(
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
      ),
    );
    await tester.pumpAndSettle();
  }

  group('grouping', () {
    test('mine first, then unwatched, then watched; each played oldest first', () {
      final stories = [
        Story.fromJson(story('a2', 'u2', 'Wanjiku', 'later', created: '2026-11-04T10:00:00Z', seen: true)),
        Story.fromJson(story('b1', 'u3', 'Kamau', 'new', created: '2026-11-04T07:00:00Z')),
        Story.fromJson(story('a1', 'u2', 'Wanjiku', 'earlier', created: '2026-11-04T06:00:00Z', seen: true)),
        Story.fromJson(story('m1', 'u1', 'Amina', 'mine', seen: true)),
      ];
      final groups = groupStories(stories, myId: 'u1');
      expect([for (final g in groups) g.author.id], ['u1', 'u3', 'u2']);
      expect([for (final s in groups.last.stories) s.id], ['a1', 'a2']);
    });
  });

  testWidgets('the strip shows neighbours, and watching marks a story seen', (tester) async {
    live = [story('s1', 'u2', 'Wanjiku M.', 'Water is back on in Rware')];
    await openApp(tester);
    expect(find.text('Your story'), findsOneWidget);
    expect(find.text('Wanjiku'), findsOneWidget);

    await tester.tap(find.text('Wanjiku'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Water is back on in Rware'), findsOneWidget);
    expect(adapter.requests.where((r) => r.path == '/stories/s1/view'), hasLength(1));

    // Five seconds on, the last story ends and the viewer closes.
    await tester.pump(const Duration(seconds: 6));
    await tester.pumpAndSettle();
    expect(find.text('Water is back on in Rware'), findsNothing);
  });

  testWidgets('my own story shows its views and who watched', (tester) async {
    live = [story('s-mine', 'u1', 'Amina K.', 'Selling mandazi today', seen: true, views: 1)];
    await openApp(tester);
    await tester.tap(find.text('Your story'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('1 view'), findsOneWidget);
    expect(adapter.requests.where((r) => r.path.endsWith('/view')), isEmpty);

    await tester.tap(find.text('1 view'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 500));
    expect(find.text('Wanjiku M.'), findsOneWidget);
  });

  testWidgets('posting a text story', (tester) async {
    live = [];
    await openApp(tester);
    await tester.tap(find.text('Your story'));
    await tester.pumpAndSettle();
    expect(find.text('New story'), findsOneWidget);

    await tester.enterText(find.byType(TextField), 'Mandazi at the stage');
    await tester.ensureVisible(find.text('Share story'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Share story'));
    await tester.pumpAndSettle();
    final post = adapter.requests.singleWhere((r) => r.path == '/stories' && r.method == 'POST');
    expect(post.data, {'kind': 'text', 'text': 'Mandazi at the stage'});
    expect(find.text('New story'), findsNothing);
  });
}
