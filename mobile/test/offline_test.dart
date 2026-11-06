import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/api_exception.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/offline_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/auth/auth_controller.dart';
import 'package:myradius/features/chats/chat_repository.dart';
import 'package:myradius/features/feed/feed_repository.dart';
import 'package:myradius/main.dart';

import 'chats_test.dart' show conversationJson, meJson, messageJson;
import 'feed_test.dart' show feedJson, postJson;
import 'support/fake_adapter.dart';
import 'support/fake_location.dart';
import 'support/fake_socket.dart';

/// A server that can be unplugged.
class Network {
  bool up = true;
}

void main() {
  late Network network;
  late FakeAdapter adapter;
  late MemoryOfflineStore store;

  FakeReply serve(RequestOptions request) {
    if (!network.up) return FakeReply.unreachable;
    final path = request.path;
    if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
    if (path == '/me') return const FakeReply(200, meJson);
    if (path == '/feed') return FakeReply(200, feedJson([postJson('p1', body: 'Water is off on Saturday')]));
    if (path == '/stories') return const FakeReply(200, {'items': [], 'next_cursor': null});
    if (path == '/conversations') {
      final folder = request.queryParameters['folder'];
      return FakeReply(200, {
        'items': folder == 'chats' ? [conversationJson(lastBody: 'See you at the stage')] : [],
        'next_cursor': null,
      });
    }
    if (path == '/conversations/c1') return FakeReply(200, conversationJson(lastBody: 'See you at the stage'));
    if (path == '/conversations/c1/messages') {
      return FakeReply(200, {
        'items': [messageJson(1, body: 'Habari'), messageJson(2, body: 'See you at the stage')],
        'has_more': false,
      });
    }
    return FakeReply(404, errorBody('not_found', 'Resource not found'));
  }

  setUp(() {
    network = Network();
    store = MemoryOfflineStore();
    adapter = FakeAdapter(serve);
  });

  ApiClient client(TokenStore tokens) => ApiClient(store: tokens, dio: fakeDio(adapter));

  group('repositories', () {
    test('a good answer is saved, and recalled when the server cannot be reached', () async {
      final api = client(MemoryTokenStore()..refreshToken = 'r1');
      final feed = FeedRepository(api, store);
      final fresh = await feed.feed();
      await Future<void>.delayed(Duration.zero);
      expect(fresh.items.single.id, 'p1');

      network.up = false;
      final saved = await feed.feed();
      expect(saved.items.single.body, 'Water is off on Saturday');
      expect(saved.nextCursor, isNull);
    });

    test('with nothing saved, being offline is still an error', () async {
      network.up = false;
      final feed = FeedRepository(client(MemoryTokenStore()), store);
      await expectLater(feed.feed(), throwsA(isA<ApiException>().having((e) => e.isNetwork, 'network', true)));
    });

    test('a chat keeps at most its latest 50 messages', () async {
      final chats = ChatRepository(client(MemoryTokenStore()..refreshToken = 'r1'), store);
      await chats.messages('c1');
      await Future<void>.delayed(Duration.zero);
      final saved = await store.read('messages_c1') as Map;
      expect((saved['items'] as List).length, 2);
      expect(ChatRepository.savedMessages, 50);
      expect(ChatRepository.savedChats, 30);
    });

    test('older pages and catching up are never served from the saved copy', () async {
      final chats = ChatRepository(client(MemoryTokenStore()..refreshToken = 'r1'), store);
      await chats.messages('c1');
      network.up = false;
      await expectLater(chats.messages('c1', afterSeq: 2), throwsA(isA<ApiException>()));
      await expectLater(chats.messages('c1', beforeSeq: 1), throwsA(isA<ApiException>()));
    });
  });

  group('the app', () {
    Future<void> open(WidgetTester tester, TokenStore tokens) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      final api = client(tokens);
      api.onReachability = (_) {};
      await tester.pumpWidget(ProviderScope(
        overrides: [
          tokenStoreProvider.overrideWithValue(tokens),
          apiClientProvider.overrideWithValue(api),
          offlineStoreProvider.overrideWithValue(store),
          locationReaderProvider.overrideWithValue(const FakeReader()),
          realtimeProvider.overrideWith((ref) {
            final realtime = RealtimeClient(api, connect: FakeSockets().connect, uri: Uri.parse('ws://api.test/v1/ws'));
            ref.onDispose(realtime.dispose);
            return realtime;
          }),
        ],
        child: const MyRadiusApp(),
      ));
      await tester.pumpAndSettle();
    }

    testWidgets('opens offline on what was saved: the feed and a chat', (tester) async {
      // Saved by an earlier, connected session.
      await store.write('me', meJson);
      await store.write('feed', feedJson([postJson('p1', body: 'Water is off on Saturday')]));
      await store.write('chats_chats', {
        'items': [conversationJson(lastBody: 'See you at the stage')],
        'next_cursor': null,
      });
      for (final folder in ['requests', 'invitations']) {
        await store.write('chats_$folder', {'items': <Object>[], 'next_cursor': null});
      }
      await store.write('conversation_c1', conversationJson(lastBody: 'See you at the stage'));
      await store.write('messages_c1', {
        'items': [messageJson(1, body: 'Habari'), messageJson(2, body: 'See you at the stage')],
        'has_more': false,
      });
      network.up = false;

      await open(tester, MemoryTokenStore()..refreshToken = 'r1');
      expect(find.text('Water is off on Saturday'), findsOneWidget);

      await tester.tap(find.text('Chats'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Wanjiku M.').first);
      await tester.pumpAndSettle();
      expect(find.text('Habari'), findsOneWidget);
    });

    testWidgets('offline with nothing saved asks to try again', (tester) async {
      network.up = false;
      await open(tester, MemoryTokenStore()..refreshToken = 'r1');
      expect(find.text("You're offline"), findsOneWidget);
    });

    testWidgets('signing out wipes what was saved', (tester) async {
      await open(tester, MemoryTokenStore()..refreshToken = 'r1');
      await tester.pump(const Duration(milliseconds: 50));
      expect(store.saved, isNotEmpty);

      final container = ProviderScope.containerOf(tester.element(find.byType(MyRadiusApp)));
      final signedOut = container.read(authControllerProvider.notifier).signOut();
      for (var i = 0; i < 20; i++) {
        await tester.pump(const Duration(milliseconds: 100));
      }
      await signedOut;
      expect(store.saved, isEmpty);
    });
  });
}
