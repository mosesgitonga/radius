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
import 'package:myradius/features/groups/group_models.dart';
import 'package:myradius/features/groups/screens/group_admin_screens.dart';
import 'package:myradius/features/groups/screens/group_screen.dart';
import 'package:myradius/features/groups/screens/groups_screen.dart';

import 'feed_test.dart' show postJson;
import 'support/fake_adapter.dart';

Map<String, dynamic> groupJson(
  String id,
  String name, {
  String visibility = 'public',
  String joinPolicy = 'open',
  bool approvesPosts = false,
  Map<String, dynamic>? membership,
  int members = 12,
}) =>
    {
      'id': id,
      'name': name,
      'description': 'Neighbours of $name',
      'category': 'estate',
      'rules': null,
      'visibility': visibility,
      'join_policy': joinPolicy,
      'requires_post_approval': approvesPosts,
      'cover': null,
      'member_count': members,
      'area_label': visibility == 'public' ? {'label': 'Kamakwa', 'precision': 'estate'} : null,
      'distance_bucket': visibility == 'public' ? '1_3km' : null,
      'my_membership': membership,
      'chat_conversation_id': null,
      'created_at': '2026-11-01T08:00:00Z',
    };

Map<String, dynamic> member(String role) => {'role': role, 'status': 'active'};

const meJson = {
  'id': 'u1',
  'display_name': 'Amina K.',
  'status': 'active',
  'has_home_location': true,
  'is_verified_neighbour': false,
  'area_label': {'label': 'Nyeri Town', 'precision': 'town'},
};

void main() {
  group('invite links', () {
    test('the token comes out of a pasted link or a bare code', () {
      expect(inviteTokenFrom('Join us: https://myradius.chatara.co/g/AbC_12-xyz789 thanks'), 'AbC_12-xyz789');
      expect(inviteTokenFrom('  AbC_12-xyz789 '), 'AbC_12-xyz789');
      expect(inviteTokenFrom('hello there'), isNull);
      expect(inviteTokenFrom(''), isNull);
    });

    test('a group knows where the caller stands', () {
      final admin = Group.fromJson(groupJson('g1', 'Kamakwa', membership: member('admin')));
      expect(admin.isMember && admin.isAdmin && admin.isModerator, isTrue);
      expect(admin.meta, 'Kamakwa · 1–3 km · 12 members');
      final asked = Group.fromJson(groupJson('g2', 'Choir', membership: {'role': 'member', 'status': 'pending'}));
      expect(asked.isMember, isFalse);
      expect(asked.hasAsked, isTrue);
      expect(asked.role, isNull);
      final secret = Group.fromJson(groupJson('g3', 'Chama', visibility: 'private'));
      expect(secret.meta, '12 members');
    });
  });

  group('screens', () {
    late List<RequestOptions> sent;
    late FakeReply Function(RequestOptions request) answer;

    Future<void> open(WidgetTester tester, String location, List<RouteBase> routes) async {
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
      final router = GoRouter(initialLocation: location, routes: routes);
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
    }

    final notFound = FakeReply(404, errorBody('not_found', 'Resource not found'));

    testWidgets('groups home: invitations, your groups, and nearby ones you are not in', (tester) async {
      answer = (request) => switch ((request.method, request.path)) {
            ('GET', '/groups/mine') => FakeReply(200, {
                'items': [groupJson('g1', 'Kamakwa Residents', membership: member('member'))],
                'next_cursor': null,
              }),
            ('GET', '/groups/invitations') => FakeReply(200, [
                groupJson('g2', 'Sunday Choir', visibility: 'private', membership: {'role': 'member', 'status': 'invited'}),
              ]),
            ('GET', '/groups/discover') => FakeReply(200, {
                'items': [
                  groupJson('g1', 'Kamakwa Residents', membership: member('member')),
                  groupJson('g3', 'Ruringu Football'),
                ],
                'next_cursor': null,
                'radius_km': 6,
              }),
            ('POST', '/groups/g2/invitation') => FakeReply(
                200, groupJson('g2', 'Sunday Choir', visibility: 'private', membership: member('member'))),
            ('GET', '/groups/g2') =>
              FakeReply(200, groupJson('g2', 'Sunday Choir', visibility: 'private', membership: member('member'))),
            ('GET', '/groups/g2/posts') => const FakeReply(200, {'items': [], 'pinned': [], 'next_cursor': null}),
            _ => notFound,
          };
      await open(tester, '/groups', [
        GoRoute(path: '/groups', builder: (_, _) => const GroupsScreen()),
        GoRoute(path: '/feed/groups/g/:id', builder: (_, s) => GroupScreen(groupId: s.pathParameters['id']!)),
      ]);

      expect(find.text('Invitations'), findsOneWidget);
      expect(find.text('Sunday Choir'), findsOneWidget);
      // Kamakwa shows once, under your groups, not again under Nearby.
      expect(find.text('Kamakwa Residents'), findsOneWidget);
      expect(find.text('Ruringu Football'), findsOneWidget);
      expect(find.text('Within 6 km of home'), findsOneWidget);

      await tester.tap(find.byTooltip('Accept'));
      await tester.pumpAndSettle();
      expect(sent.any((r) => r.path == '/groups/g2/invitation' && (r.data as Map)['action'] == 'accept'), isTrue);
      expect(find.text('Write a post'), findsOneWidget);
    });

    testWidgets('joining an open group lets you post in it', (tester) async {
      var joined = false;
      answer = (request) => switch ((request.method, request.path)) {
            ('GET', '/groups/g3') => FakeReply(200, groupJson('g3', 'Ruringu Football', membership: joined ? member('member') : null)),
            ('GET', '/groups/g3/posts') => FakeReply(200, {
                'items': [postJson('p1', body: 'Training moved to 5pm')],
                'pinned': <Object>[],
                'next_cursor': null,
              }),
            ('POST', '/groups/g3/join') => () {
                joined = true;
                return FakeReply(200, groupJson('g3', 'Ruringu Football', membership: member('member')));
              }(),
            _ => notFound,
          };
      await open(tester, '/g', [GoRoute(path: '/g', builder: (_, _) => const GroupScreen(groupId: 'g3'))]);

      expect(find.text('Training moved to 5pm'), findsOneWidget);
      expect(find.byTooltip('Post in the group'), findsNothing);

      await tester.tap(find.text('Join group'));
      await tester.pumpAndSettle();
      expect(sent.any((r) => r.method == 'POST' && r.path == '/groups/g3/join'), isTrue);
      expect(find.byTooltip('Post in the group'), findsOneWidget);
    });

    testWidgets('a private group you are not in keeps its posts to itself', (tester) async {
      answer = (request) => switch ((request.method, request.path)) {
            ('GET', '/groups/g4') => FakeReply(200, groupJson('g4', 'Chama ya Wamama', visibility: 'private')),
            _ => notFound,
          };
      await open(tester, '/g', [GoRoute(path: '/g', builder: (_, _) => const GroupScreen(groupId: 'g4'))]);

      expect(find.textContaining('This group is private'), findsOneWidget);
      expect(find.text("Posts are shown to the group's members."), findsOneWidget);
      expect(sent.any((r) => r.path == '/groups/g4/posts'), isFalse);
    });

    testWidgets('posting in a group sends it to the group, not the feed', (tester) async {
      answer = (request) => switch ((request.method, request.path)) {
            ('POST', '/groups/g1/posts') => FakeReply(201, {
                ...postJson('p9', body: 'Water off on Saturday'),
                'group_id': 'g1',
                'approval_status': 'pending',
              }),
            _ => notFound,
          };
      Post? posted;
      await open(tester, '/', [
        GoRoute(
          path: '/',
          builder: (context, _) => Scaffold(
            body: TextButton(
              onPressed: () async => posted = await context.push<Post>('/compose'),
              child: const Text('write'),
            ),
          ),
        ),
        GoRoute(
          path: '/compose',
          builder: (_, _) => const ComposeScreen(groupId: 'g1', groupName: 'Kamakwa Residents'),
        ),
      ]);
      await tester.tap(find.text('write'));
      await tester.pumpAndSettle();
      expect(find.text('Post to Kamakwa Residents'), findsOneWidget);

      await tester.enterText(find.byType(TextField), 'Water off on Saturday');
      await tester.tap(find.text('Post'));
      await tester.pumpAndSettle();

      final request = sent.singleWhere((r) => r.method == 'POST' && r.path.startsWith('/groups'));
      expect(request.path, '/groups/g1/posts');
      expect((request.data as Map)['body'], 'Water off on Saturday');
      expect(sent.any((r) => r.path == '/posts'), isFalse);
      expect(posted?.awaitsApproval, isTrue);
      expect(find.textContaining('once a moderator approves it'), findsOneWidget);
    });

    testWidgets('an admin lets in someone asking to join', (tester) async {
      var asking = true;
      answer = (request) {
        final path = request.path;
        if (request.method == 'GET' && path == '/groups/g1') {
          return FakeReply(200, groupJson('g1', 'Kamakwa Residents', joinPolicy: 'approval', membership: member('admin')));
        }
        if (request.method == 'GET' && path == '/groups/g1/members') {
          final pending = request.queryParameters['status'] == 'pending';
          Map<String, dynamic> person(String id, String name, String role, String status) => {
                'user': {'id': id, 'display_name': name, 'is_verified_neighbour': false},
                'role': role,
                'status': status,
              };
          return FakeReply(200, {
            'items': pending
                ? [if (asking) person('u7', 'Otieno J.', 'member', 'pending')]
                : [person('u1', 'Amina K.', 'admin', 'active'), person('u5', 'Wanjiku M.', 'member', 'active')],
            'next_cursor': null,
          });
        }
        if (request.method == 'POST' && path == '/groups/g1/members/u7/review') {
          asking = false;
          return const FakeReply(204);
        }
        return notFound;
      };
      await open(tester, '/m', [GoRoute(path: '/m', builder: (_, _) => const GroupMembersScreen(groupId: 'g1'))]);

      expect(find.text('Asking to join'), findsOneWidget);
      expect(find.text('Otieno J.'), findsOneWidget);
      // An admin can remove a member, but not themselves.
      expect(find.byTooltip('Remove from group'), findsOneWidget);

      await tester.tap(find.byTooltip('Let in'));
      await tester.pumpAndSettle();
      final review = sent.singleWhere((r) => r.path == '/groups/g1/members/u7/review');
      expect(review.data, {'action': 'approve'});
      expect(find.text('Asking to join'), findsNothing);
    });
  });
}
