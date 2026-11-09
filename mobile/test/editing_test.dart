import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:go_router/go_router.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/offline_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/theme.dart';
import 'package:myradius/features/feed/feed_models.dart';
import 'package:myradius/features/feed/screens/compose_screen.dart';
import 'package:myradius/features/feed/screens/post_screen.dart';

import 'comments_test.dart' show commentJson;
import 'feed_test.dart' show postJson;
import 'support/fake_adapter.dart';

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

void main() {
  late List<RequestOptions> sent;
  late FakeReply Function(RequestOptions request) answer;
  final notFound = FakeReply(404, errorBody('not_found', 'Resource not found'));

  /// A home screen with a button into post p1, so the post screen can close.
  Future<void> open(WidgetTester tester) async {
    tester.view.physicalSize = const Size(1080, 2400);
    tester.view.devicePixelRatio = 2.75;
    addTearDown(tester.view.reset);
    final store = MemoryTokenStore()..refreshToken = 'r1';
    final adapter = FakeAdapter((request) => switch ((request.method, request.path)) {
          ('POST', '/auth/refresh') => FakeReply(200, tokens('a1', 'r2')),
          ('GET', '/me') => const FakeReply(200, meJson),
          _ => answer(request),
        });
    sent = adapter.requests;
    final api = ApiClient(store: store, dio: fakeDio(adapter));
    final router = GoRouter(routes: [
      GoRoute(
        path: '/',
        builder: (context, _) => Scaffold(
          body: TextButton(onPressed: () => context.push('/feed/post/p1'), child: const Text('open post')),
        ),
      ),
      GoRoute(path: '/feed/post/:id', builder: (_, s) => PostScreen(postId: s.pathParameters['id']!)),
      GoRoute(path: '/posts/:id/edit', builder: (_, s) => ComposeScreen(editing: s.extra as Post?)),
    ]);
    addTearDown(router.dispose);
    await tester.pumpWidget(ProviderScope(
      overrides: [
        tokenStoreProvider.overrideWithValue(store),
        apiClientProvider.overrideWithValue(api),
        offlineStoreProvider.overrideWithValue(MemoryOfflineStore()),
      ],
      child: MaterialApp.router(theme: buildTheme(), routerConfig: router),
    ));
    await tester.pumpAndSettle();
    await tester.tap(find.text('open post'));
    await tester.pumpAndSettle();
  }

  Map<String, dynamic> comments(List<Map<String, dynamic>> items) => {'items': items, 'next_cursor': null};

  testWidgets('editing your post changes its text and marks it edited', (tester) async {
    var edited = false;
    answer = (request) => switch ((request.method, request.path)) {
          ('GET', '/posts/p1') => FakeReply(200, postJson('p1', body: 'Water off on Saturdy', authorId: 'u1')),
          ('GET', '/posts/p1/comments') => FakeReply(200, comments([])),
          ('PATCH', '/posts/p1') => () {
              edited = true;
              return FakeReply(200, {
                ...postJson('p1', body: 'Water off on Saturday', category: 'alert', authorId: 'u1'),
                'edited_at': '2026-11-09T09:00:00Z',
              });
            }(),
          _ => notFound,
        };
    await open(tester);

    await tester.tap(find.byTooltip('More'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Edit post'));
    await tester.pumpAndSettle();
    expect(find.text('Edit post'), findsOneWidget);
    expect(find.text('Water off on Saturdy'), findsOneWidget);

    await tester.enterText(find.byType(TextField), 'Water off on Saturday');
    await tester.tap(find.text('Save'));
    await tester.pumpAndSettle();

    expect(edited, isTrue);
    final patch = sent.singleWhere((r) => r.method == 'PATCH');
    expect(patch.data, {'body': 'Water off on Saturday', 'category': 'alert'});
    expect(find.text('Water off on Saturday'), findsOneWidget);
    expect(find.textContaining('edited'), findsOneWidget);
  });

  testWidgets('deleting your post asks first, then closes it', (tester) async {
    answer = (request) => switch ((request.method, request.path)) {
          ('GET', '/posts/p1') => FakeReply(200, postJson('p1', authorId: 'u1')),
          ('GET', '/posts/p1/comments') => FakeReply(200, comments([])),
          ('DELETE', '/posts/p1') => const FakeReply(204),
          _ => notFound,
        };
    await open(tester);

    await tester.tap(find.byTooltip('More'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Delete post'));
    await tester.pumpAndSettle();
    expect(find.text('Delete this post?'), findsOneWidget);
    await tester.tap(find.text('Delete'));
    await tester.pumpAndSettle();

    expect(sent.where((r) => r.method == 'DELETE' && r.path == '/posts/p1'), hasLength(1));
    expect(find.text('open post'), findsOneWidget);
  });

  testWidgets("someone else's post: no edit or delete", (tester) async {
    answer = (request) => switch ((request.method, request.path)) {
          ('GET', '/posts/p1') => FakeReply(200, postJson('p1', authorId: 'u2')),
          ('GET', '/posts/p1/comments') => FakeReply(200, comments([])),
          _ => notFound,
        };
    await open(tester);

    await tester.tap(find.byTooltip('More'));
    await tester.pumpAndSettle();

    expect(find.text('Edit post'), findsNothing);
    expect(find.text('Delete post'), findsNothing);
    expect(find.text('Report post'), findsOneWidget);
  });

  testWidgets('you delete your own comment; on your post you can delete anyone’s', (tester) async {
    var deleted = <String>[];
    answer = (request) {
      if (request.method == 'GET' && request.path == '/posts/p1') {
        return FakeReply(200, postJson('p1', authorId: 'u1', comments: 2));
      }
      if (request.method == 'GET' && request.path == '/posts/p1/comments') {
        return FakeReply(200, comments([
          for (final c in [
            commentJson('c1', 'Rude remark', authorId: 'u2'),
            commentJson('c2', 'My own note', authorId: 'u1', name: 'Amina K.'),
          ])
            if (!deleted.contains(c['id'])) c,
        ]));
      }
      if (request.method == 'DELETE' && request.path.startsWith('/comments/')) {
        deleted = [...deleted, request.path.split('/').last];
        return const FakeReply(204);
      }
      return notFound;
    };
    await open(tester);

    // Someone else's comment under my post: delete, and report.
    await tester.tap(find.byTooltip('Comment options').first);
    await tester.pumpAndSettle();
    expect(find.text('Report comment'), findsOneWidget);
    await tester.tap(find.text('Delete comment'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Delete'));
    await tester.pumpAndSettle();
    expect(deleted, ['c1']);
    expect(find.text('Rude remark'), findsNothing);

    // My own: delete, no report.
    await tester.tap(find.byTooltip('Comment options'));
    await tester.pumpAndSettle();
    expect(find.text('Report comment'), findsNothing);
    await tester.tap(find.text('Delete comment'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('Delete'));
    await tester.pumpAndSettle();
    expect(deleted, ['c1', 'c2']);
    expect(find.text('No comments yet'), findsOneWidget);
  });

  testWidgets("on someone else's post, others' comments can only be reported", (tester) async {
    answer = (request) => switch ((request.method, request.path)) {
          ('GET', '/posts/p1') => FakeReply(200, postJson('p1', authorId: 'u2', comments: 1)),
          ('GET', '/posts/p1/comments') => FakeReply(200, comments([commentJson('c1', 'A tip', authorId: 'u3')])),
          _ => notFound,
        };
    await open(tester);

    await tester.tap(find.byTooltip('Comment options'));
    await tester.pumpAndSettle();

    expect(find.text('Delete comment'), findsNothing);
    expect(find.text('Report comment'), findsOneWidget);
  });
}
