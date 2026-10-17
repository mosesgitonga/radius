import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/features/chats/chat_models.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

Map<String, dynamic> author(String id, String name) =>
    {'id': id, 'display_name': name, 'is_verified_neighbour': false};

Map<String, dynamic> messageJson(int seq, {String from = 'u2', String? body, bool deleted = false}) => {
      'id': 'm$seq',
      'conversation_id': 'c1',
      'seq': seq,
      'kind': 'text',
      'sender': from == 'u1' ? author('u1', 'Amina K.') : author('u2', 'Wanjiku M.'),
      'body': deleted ? null : (body ?? 'Message $seq'),
      'deleted': deleted,
      'created_at': '2026-10-17T09:0$seq:00Z',
    };

Map<String, dynamic> conversationJson({
  int lastSeq = 2,
  int unread = 0,
  int lastRead = 2,
  String state = 'active',
  String myStatus = 'active',
  int? otherRead,
  String? lastBody,
}) =>
    {
      'id': 'c1',
      'kind': 'direct',
      'state': state,
      'my_role': 'member',
      'my_status': myStatus,
      'muted': false,
      'other_user': author('u2', 'Wanjiku M.'),
      'other_last_read_seq': otherRead,
      'member_count': 2,
      'last_seq': lastSeq,
      'last_read_seq': lastRead,
      'unread_count': unread,
      'last_message': messageJson(lastSeq, body: lastBody),
      'last_message_at': '2026-10-17T09:0$lastSeq:00Z',
      'created_at': '2026-10-17T09:00:00Z',
      'updated_at': '2026-10-17T09:0$lastSeq:00Z',
    };

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

void main() {
  group('models', () {
    test('system messages are written out from current names', () {
      const names = {'u1': 'Amina K.', 'u2': 'Wanjiku M.', 'u3': 'Otieno'};
      expect(systemText({'event': 'member_joined', 'user_id': 'u3'}, names: names), 'Otieno joined');
      expect(
        systemText({'event': 'members_added', 'actor_id': 'u1', 'user_ids': ['u2', 'u3']}, names: names),
        'Amina K. added Wanjiku M. and Otieno',
      );
      expect(systemText({'event': 'member_left', 'user_id': 'gone'}, names: names), 'Someone left');
      expect(
        systemText({'event': 'role_changed', 'actor_id': 'u1', 'user_id': 'u3', 'role': 'admin'}, names: names),
        'Amina K. made Otieno an admin',
      );
    });

    test('previews say who wrote, and never show what was deleted', () {
      final mine = Message.fromJson(messageJson(1, from: 'u1', body: 'See you'));
      final theirs = Message.fromJson(messageJson(2, body: 'Asante'));
      final deleted = Message.fromJson(messageJson(3, deleted: true));
      expect(messagePreview(mine, myId: 'u1'), 'You: See you');
      expect(messagePreview(theirs, myId: 'u1'), 'Asante');
      expect(messagePreview(deleted, myId: 'u1'), 'Message deleted');
    });

    test('days read as people say them', () {
      final now = DateTime(2026, 10, 17, 12);
      expect(dayLabel(DateTime(2026, 10, 17, 8), now: now), 'Today');
      expect(dayLabel(DateTime(2026, 10, 16, 23), now: now), 'Yesterday');
      expect(dayLabel(DateTime(2026, 10, 13, 9), now: now), 'Tuesday');
      expect(dayLabel(DateTime(2026, 9, 1, 9), now: now), '1 Sep');
      expect(dayLabel(DateTime(2025, 9, 1, 9), now: now), '1 Sep 2025');
    });

    test('a conversation knows which folder it belongs in', () {
      expect(Conversation.fromJson(conversationJson()).awaitsMe, isFalse);
      expect(Conversation.fromJson(conversationJson(state: 'request_received')).awaitsMe, isTrue);
      expect(Conversation.fromJson(conversationJson(myStatus: 'invited')).awaitsMe, isTrue);
      expect(Conversation.fromJson(conversationJson(myStatus: 'left')).isClosed, isTrue);
    });
  });

  group('realtime', () {
    late FakeSockets sockets;
    late ApiClient api;
    late RealtimeClient client;
    late int refreshes;

    setUp(() async {
      sockets = FakeSockets();
      refreshes = 0;
      final adapter = FakeAdapter((request) {
        if (request.path == '/auth/refresh') {
          refreshes++;
          return FakeReply(200, tokens('a${refreshes + 1}', 'r${refreshes + 1}'));
        }
        return FakeReply(404, errorBody('not_found', 'Resource not found'));
      });
      api = ApiClient(store: MemoryTokenStore(), dio: fakeDio(adapter));
      await api.startSession(const TokenPair(accessToken: 'a1', refreshToken: 'r1'));
      client = RealtimeClient(api, connect: sockets.connect, uri: Uri.parse('ws://api.test/v1/ws'));
    });

    testWidgets('signs in by header, pings, and passes events on', (tester) async {
      final events = <String>[];
      client.events.listen((e) => events.add(e.type));
      client.start();
      await tester.pump();
      expect(sockets.last.headers['Authorization'], 'Bearer a1');

      sockets.last.sendReady(heartbeatSeconds: 5);
      sockets.last.push('message.created', {'conversation_id': 'c1', 'seq': 3, 'message_id': 'm3'});
      await tester.pump();
      expect(events, [RealtimeEvent.connected, 'message.created']);
      expect(client.connected.value, isTrue);

      await tester.pump(const Duration(seconds: 5));
      expect(sockets.last.sent.last, {'type': 'ping'});
      client.dispose();
    });

    testWidgets('sends a fresh token a minute before the old one expires', (tester) async {
      client.start();
      await tester.pump();
      sockets.last.sendReady(expiresAt: DateTime.now().add(const Duration(seconds: 90)));
      await tester.pump();
      await tester.pump(const Duration(seconds: 31));
      await tester.pump();
      expect(sockets.last.sent.last, {'type': 'auth', 'token': 'a2'});
      expect(sockets.opened, hasLength(1));
      client.dispose();
    });

    testWidgets('reconnects after a drop, renewing the sign-in after a 4401', (tester) async {
      final events = <String>[];
      client.events.listen((e) => events.add(e.type));
      client.start();
      await tester.pump();
      sockets.last.sendReady();
      await tester.pump();

      await sockets.last.serverClose(4401);
      await tester.pump();
      expect(client.connected.value, isFalse);
      await tester.pump(const Duration(seconds: 2));
      await tester.pump();
      expect(sockets.opened, hasLength(2));
      expect(refreshes, 1);
      expect(sockets.last.headers['Authorization'], 'Bearer a2');

      // The new connection's ready is a cue to catch up.
      sockets.last.sendReady();
      await tester.pump();
      expect(events.where((e) => e == RealtimeEvent.connected), hasLength(2));
      client.dispose();
    });

    testWidgets('a banned account stops reconnecting', (tester) async {
      client.start();
      await tester.pump();
      await sockets.last.serverClose(4403);
      await tester.pump(const Duration(minutes: 1));
      expect(sockets.opened, hasLength(1));
      client.dispose();
    });
  });

  group('screens', () {
    void usePhoneScreen(WidgetTester tester) {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
    }

    late FakeAdapter adapter;
    late FakeSockets sockets;
    late List<Map<String, dynamic>> messages;
    late Map<String, dynamic> conversation;
    var failSends = false;

    FakeReply handle(RequestOptions request) {
      final path = request.path;
      final method = request.method;
      if (path == '/auth/refresh') return FakeReply(200, tokens('a1', 'r2'));
      if (path == '/me') return const FakeReply(200, meJson);
      if (path == '/feed') return const FakeReply(200, {'items': [], 'next_cursor': null, 'radius_km': 15});
      if (path == '/conversations') {
        final folder = request.queryParameters['folder'];
        return FakeReply(200, {'items': folder == 'chats' ? [conversation] : [], 'next_cursor': null});
      }
      if (path == '/conversations/c1') return FakeReply(200, conversation);
      if (path == '/conversations/c1/read') return const FakeReply(204);
      if (path == '/conversations/c1/messages' && method == 'GET') {
        final after = request.queryParameters['after_seq'] as int?;
        return FakeReply(200, {
          'items': [for (final m in messages) if (after == null || (m['seq'] as int) > after) m],
          'has_more': false,
        });
      }
      if (path == '/conversations/c1/messages' && method == 'POST') {
        if (failSends) return FakeReply(503, errorBody('unavailable', 'Try again shortly'));
        final sent = messageJson(messages.length + 1, from: 'u1', body: (request.data as Map)['body'] as String);
        messages.add(sent);
        return FakeReply(201, sent);
      }
      return FakeReply(404, errorBody('not_found', 'Resource not found'));
    }

    Future<void> openApp(WidgetTester tester) async {
      usePhoneScreen(tester);
      final store = MemoryTokenStore()..refreshToken = 'r1';
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      await tester.pumpWidget(ProviderScope(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(api),
          realtimeProvider.overrideWith((ref) {
            final client = RealtimeClient(api, connect: sockets.connect, uri: Uri.parse('ws://api.test/v1/ws'));
            ref.onDispose(client.dispose);
            return client;
          }),
        ],
        child: const MyRadiusApp(),
      ));
      await tester.pumpAndSettle();
    }

    setUp(() {
      failSends = false;
      sockets = FakeSockets();
      messages = [messageJson(1, from: 'u1', body: 'Is the stage moving?'), messageJson(2, body: 'Yes, from Monday')];
      conversation = conversationJson(unread: 1, lastRead: 1, lastBody: 'Yes, from Monday');
      adapter = FakeAdapter(handle);
    });

    testWidgets('the inbox shows unread, and a live message arrives by the doorbell', (tester) async {
      await openApp(tester);
      // The socket is open while signed in.
      expect(sockets.opened, hasLength(1));
      // The unread count is on the Chats tab.
      expect(find.descendant(of: find.byType(Badge), matching: find.text('1')), findsOneWidget);

      await tester.tap(find.text('Chats').last);
      await tester.pumpAndSettle();
      expect(find.text('Wanjiku M.'), findsOneWidget);
      expect(find.text('Yes, from Monday'), findsOneWidget);

      await tester.tap(find.text('Wanjiku M.'));
      await tester.pumpAndSettle();
      expect(find.text('Is the stage moving?'), findsOneWidget);
      // Opening it marks it read up to the newest message.
      final read = adapter.requests.lastWhere((r) => r.path == '/conversations/c1/read');
      expect(read.data, {'seq': 2});

      // Someone writes: the socket says so, and the app fetches what is new.
      messages.add(messageJson(3, body: 'Pole for the late reply'));
      sockets.last.sendReady();
      sockets.last.push('message.created', {'conversation_id': 'c1', 'seq': 3, 'message_id': 'm3'});
      await tester.pumpAndSettle();
      expect(find.text('Pole for the late reply'), findsOneWidget);
      final fetch = adapter.requests.firstWhere(
          (r) => r.path == '/conversations/c1/messages' && r.queryParameters['after_seq'] != null);
      expect(fetch.queryParameters['after_seq'], 2);
    });

    testWidgets('a message that fails to send is kept, and the retry is the same request', (tester) async {
      await openApp(tester);
      await tester.tap(find.text('Chats').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Wanjiku M.'));
      await tester.pumpAndSettle();

      failSends = true;
      await tester.enterText(find.byType(TextField), 'Thank you');
      await tester.tap(find.byTooltip('Send'));
      await tester.pumpAndSettle();
      expect(find.text('Thank you'), findsOneWidget);
      expect(find.text('Not sent · tap for options'), findsOneWidget);
      final firstKey = adapter.requests
          .lastWhere((r) => r.path == '/conversations/c1/messages' && r.method == 'POST')
          .headers['Idempotency-Key'];

      failSends = false;
      await tester.tap(find.text('Thank you'));
      await tester.pumpAndSettle();
      await tester.tap(find.text('Try again'));
      await tester.pumpAndSettle();
      expect(find.text('Not sent · tap for options'), findsNothing);
      expect(find.text('Thank you'), findsOneWidget);
      final retryKey = adapter.requests
          .lastWhere((r) => r.path == '/conversations/c1/messages' && r.method == 'POST')
          .headers['Idempotency-Key'];
      expect(retryKey, firstKey);
    });

    testWidgets('a request waits on me: no message box until I accept', (tester) async {
      conversation = conversationJson(
          state: 'request_received', lastSeq: 1, unread: 1, lastRead: 0, lastBody: 'Hi, is the gas cooker still for sale?');
      messages = [messageJson(1, body: 'Hi, is the gas cooker still for sale?')];
      adapter = FakeAdapter((request) {
        if (request.path == '/conversations') {
          final folder = request.queryParameters['folder'];
          return FakeReply(200, {'items': folder == 'requests' ? [conversation] : [], 'next_cursor': null});
        }
        if (request.path == '/conversations/c1/request') {
          conversation = conversationJson(lastSeq: 1, lastRead: 1);
          return FakeReply(200, conversation);
        }
        return handle(request);
      });
      await openApp(tester);
      await tester.tap(find.text('Chats').last);
      await tester.pumpAndSettle();
      await tester.tap(find.text('Requests · 1'));
      await tester.pumpAndSettle();
      expect(find.text('Hi, is the gas cooker still for sale?'), findsOneWidget);

      await tester.tap(find.text('Wanjiku M.'));
      await tester.pumpAndSettle();
      expect(find.byType(TextField), findsNothing);
      expect(find.text('Block and report'), findsOneWidget);

      await tester.tap(find.text('Accept'));
      await tester.pumpAndSettle();
      expect(adapter.requests.any((r) => r.path == '/conversations/c1/request' && (r.data as Map)['action'] == 'accept'),
          isTrue);
      expect(find.byType(TextField), findsOneWidget);
    });
  });
}
