import 'package:dio/dio.dart' show RequestOptions;
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/api/api_client.dart';
import 'package:myradius/core/api/token_store.dart';
import 'package:myradius/core/app_update.dart';
import 'package:myradius/core/offline_store.dart';
import 'package:myradius/core/providers.dart';
import 'package:myradius/core/realtime.dart';
import 'package:myradius/main.dart';

import 'support/fake_adapter.dart';
import 'support/fake_socket.dart';

void main() {
  group('deciding', () {
    test('older than supported must update; older than latest may', () {
      final json = {'latest_build': 7, 'min_supported_build': 4, 'update_url': 'https://x.invalid/a.apk'};
      final old = AppUpdate.decide(json, build: 3);
      expect((old.required, old.available), (true, true));
      final behind = AppUpdate.decide(json, build: 5);
      expect((behind.required, behind.available, behind.showBanner), (false, true, true));
      final current = AppUpdate.decide(json, build: 7);
      expect((current.required, current.available), (false, false));
      expect(behind.dismiss().showBanner, isFalse);
    });

    test('nothing set means nothing to do', () {
      final none = AppUpdate.decide({'latest_build': null, 'min_supported_build': null}, build: 1);
      expect((none.required, none.available), (false, false));
    });
  });

  group('the app', () {
    late List<RequestOptions> sent;

    Future<void> open(WidgetTester tester, {required int build, required Map<String, dynamic> version}) async {
      tester.view.physicalSize = const Size(1080, 2400);
      tester.view.devicePixelRatio = 2.75;
      addTearDown(tester.view.reset);
      // Signed out: the check needs no account.
      final store = MemoryTokenStore();
      final adapter = FakeAdapter((request) => switch ((request.method, request.path)) {
            ('GET', '/app/version') => FakeReply(200, version),
            _ => FakeReply(404, errorBody('not_found', 'Resource not found')),
          });
      sent = adapter.requests;
      final api = ApiClient(store: store, dio: fakeDio(adapter));
      await tester.pumpWidget(ProviderScope(
        overrides: [
          tokenStoreProvider.overrideWithValue(store),
          apiClientProvider.overrideWithValue(api),
          offlineStoreProvider.overrideWithValue(MemoryOfflineStore()),
          appBuildProvider.overrideWithValue(build),
          realtimeProvider.overrideWith((ref) {
            final client = RealtimeClient(api, connect: FakeSockets().connect, uri: Uri.parse('ws://api.test/v1/ws'));
            ref.onDispose(client.dispose);
            return client;
          }),
        ],
        child: const MyRadiusApp(),
      ));
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 100));
    }

    testWidgets('a build too old to go on is stopped at "Update required"', (tester) async {
      await open(tester, build: 2, version: {
        'latest_build': 5,
        'min_supported_build': 3,
        'update_url': 'https://x.invalid/myradius.apk',
        'message': null,
      });

      expect(find.text('Update required'), findsOneWidget);
      expect(find.text('Update now'), findsOneWidget);
      expect(sent.single.queryParameters['platform'], 'android');
    });

    testWidgets('a build behind the latest gets a banner it can close', (tester) async {
      await open(tester, build: 4, version: {
        'latest_build': 5,
        'min_supported_build': 3,
        'update_url': 'https://x.invalid/myradius.apk',
        'message': 'New: groups, and editing your posts.',
      });

      expect(find.text('Update required'), findsNothing);
      expect(find.text('New: groups, and editing your posts.'), findsOneWidget);

      await tester.tap(find.descendant(of: find.byType(UpdateBanner), matching: find.byIcon(Icons.close_rounded)));
      await tester.pump();
      expect(find.text('New: groups, and editing your posts.'), findsNothing);
    });

    testWidgets('a development build never asks', (tester) async {
      await open(tester, build: 0, version: {'latest_build': 9, 'min_supported_build': 9});

      expect(sent.where((r) => r.path == '/app/version'), isEmpty);
      expect(find.text('Update required'), findsNothing);
    });
  });
}
