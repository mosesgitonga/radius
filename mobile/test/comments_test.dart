import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/offline_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/theme.dart';
import 'package:myradius/features/feed/feed_models.dart';
import 'package:myradius/features/feed/screens/post_screen.dart';

import 'feed_test.dart' show postJson;
import 'support/fake_adapter.dart';

Map<String, dynamic> commentJson(
  String id,
  String body, {
  String authorId = 'u2',
  String name = 'Wanjiku M.',
  String? parentId,
  int replies = 0,
  int score = 0,
  int myVote = 0,
}) =>
    {
      'id': id,
      'post_id': 'p1',
      'parent_id': parentId,
      'author': {'id': authorId, 'display_name': name, 'is_verified_neighbour': false},
      'is_visiting': false,
      'body': body,
      'reply_count': replies,
      'reaction_count': 0,
      'my_reaction': null,
      'score': score,
      'my_vote': myVote,
      'created_at': '2026-11-07T08:00:00Z',
    };

void main() {
  test('a comment reads its score, vote and thread from the API', () {
    final reply = Comment.fromJson(commentJson('r1', 'Agreed', parentId: 'c1', score: -2, myVote: -1));
    expect(reply.score, -2);
    expect(reply.myVote, -1);
    expect(reply.threadId, 'c1');
    expect(Comment.fromJson(commentJson('c1', 'Top')).threadId, 'c1');
    expect(reply.copyWith(score: 0, myVote: 0).myVote, 0);
  });

  group('post screen', () {
    late List<RequestOptions> sent;
    late FakeReply Function(RequestOptions request) answer;

    Future<void> open(WidgetTester tester) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      final store = MemoryTokenStore()..refreshToken = 'r1';
      final adapter = FakeAdapter((request) => switch ((request.method, request.path)) {
            ('POST', '/auth/refresh') => FakeReply(200, tokens('a1', 'r2')),
            ('GET', '/me') => const FakeReply(200, {
                'id': 'u1',
                'display_name': 'Amina K.',
                'status': 'active',
                'has_home_location': true,
                'is_verified_neighbour': false,
                'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
              }),
            ('GET', '/posts/p1') => FakeReply(200, postJson('p1', comments: 3)),
            _ => answer(request),
          });
      sent = adapter.requests;
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      await tester.pumpWidget(ProviderScope(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(api),
          offlineStoreProvider.overrideWithValue(MemoryOfflineStore()),
        ],
        child: MaterialApp(theme: buildTheme(), home: const PostScreen(postId: 'p1')),
      ));
      await tester.pumpAndSettle();
    }

    testWidgets('comments show their score; voting lights the arrow and a second tap takes it back',
        (tester) async {
      answer = (request) => switch ((request.method, request.path)) {
            ('GET', '/posts/p1/comments') => FakeReply(200, {
                'items': [
                  commentJson('c1', 'Try the Saturday market', score: 4),
                  commentJson('c2', 'My own tip', authorId: 'u1', name: 'Amina K.', score: 1),
                ],
                'next_cursor': null,
              }),
            ('PUT', '/comments/c1/vote') => const FakeReply(200, {'score': 5, 'my_vote': 1}),
            ('DELETE', '/comments/c1/vote') => const FakeReply(200, {'score': 4, 'my_vote': 0}),
            _ => FakeReply(404, errorBody('not_found', 'Resource not found')),
          };
      await open(tester);

      expect(find.text('Try the Saturday market'), findsOneWidget);
      expect(find.text('4'), findsOneWidget);

      await tester.tap(find.byTooltip('Vote up').first);
      await tester.pumpAndSettle();
      expect(find.text('5'), findsOneWidget);
      expect(sent.last.data, {'value': 1});

      await tester.tap(find.byTooltip('Vote up').first);
      await tester.pumpAndSettle();
      expect(find.text('4'), findsOneWidget);
      expect(sent.last.method, 'DELETE');

      // Your own comment: the arrows say why they do nothing, and send nothing.
      final before = sent.length;
      await tester.tap(find.byTooltip('Vote up').last);
      await tester.pumpAndSettle();
      expect(find.text("You can't vote on your own comment"), findsOneWidget);
      expect(sent.length, before);
    });

    testWidgets('a refused vote is put back', (tester) async {
      answer = (request) => switch ((request.method, request.path)) {
            ('GET', '/posts/p1/comments') => FakeReply(200, {
                'items': [commentJson('c1', 'Try the Saturday market', score: 4)],
                'next_cursor': null,
              }),
            ('PUT', '/comments/c1/vote') => FakeReply(403, errorBody('restricted', "You can't vote right now")),
            _ => FakeReply(404, errorBody('not_found', 'Resource not found')),
          };
      await open(tester);

      await tester.tap(find.byTooltip('Vote down'));
      await tester.pumpAndSettle();

      expect(find.text('4'), findsOneWidget);
      expect(find.text("You can't vote right now"), findsOneWidget);
    });

    testWidgets('replies open in place, and replying to a reply joins the thread', (tester) async {
      var replies = [commentJson('r1', 'Which stall?', parentId: 'c1', name: 'Otieno J.', authorId: 'u3')];
      answer = (request) {
        if (request.method == 'GET' && request.path == '/posts/p1/comments') {
          return request.queryParameters['parent_id'] == 'c1'
              ? FakeReply(200, {'items': replies, 'next_cursor': null})
              : FakeReply(200, {
                  'items': [commentJson('c1', 'Try the Saturday market', replies: 1, score: 2)],
                  'next_cursor': null,
                });
        }
        if (request.method == 'POST' && request.path == '/posts/p1/comments') {
          final made = commentJson('r2', 'The one by the bridge',
              parentId: 'c1', authorId: 'u1', name: 'Amina K.');
          replies = [...replies, made];
          return FakeReply(201, made);
        }
        return FakeReply(404, errorBody('not_found', 'Resource not found'));
      };
      await open(tester);

      await tester.tap(find.text('1 reply'));
      await tester.pumpAndSettle();
      expect(find.text('Which stall?'), findsOneWidget);
      expect(find.text('Hide replies'), findsOneWidget);

      // Reply to the reply: it goes to the top comment's thread.
      await tester.tap(find.text('Reply').last);
      await tester.pumpAndSettle();
      expect(find.text('Replying to Otieno J.'), findsOneWidget);
      await tester.enterText(find.byType(TextField), 'The one by the bridge');
      await tester.tap(find.byTooltip('Send'));
      await tester.pumpAndSettle();

      final posted = sent.lastWhere((request) => request.method == 'POST' && request.path == '/posts/p1/comments');
      expect(posted.data, {'body': 'The one by the bridge', 'parent_id': 'c1'});
      expect(find.text('The one by the bridge'), findsOneWidget);
      expect(find.text('Replying to Otieno J.'), findsNothing);
      expect(find.text('Hide replies'), findsOneWidget);

      await tester.tap(find.text('Hide replies'));
      await tester.pumpAndSettle();
      expect(find.text('2 replies'), findsOneWidget);
      expect(find.text('Which stall?'), findsNothing);
    });
  });
}
