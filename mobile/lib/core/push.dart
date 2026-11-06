// ignore_for_file: deprecated_member_use
import 'dart:async';
import 'dart:io' show Platform;

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import 'api/api_client.dart';
import 'api/api_exception.dart';
import 'providers.dart';
import 'router.dart';

/// Push notifications through Firebase Cloud Messaging.
///
/// While someone is signed in, this phone's FCM token is registered with the API,
/// which sends pushes from its worker. Signing out removes it first, so a shared
/// phone stops receiving the last person's messages. Tapping a notification opens
/// what it is about.
///
/// Does nothing when Firebase is not set up (tests, a build without
/// `google-services.json`), so the rest of the app never depends on it.
///
/// Uses FCM registration tokens (`getToken`), which firebase_messaging 17 marks
/// deprecated in favour of installation IDs (`register`/`onRegistered`). The API
/// sends with firebase-admin to registration tokens, so it stays on tokens until
/// the server can send to installation IDs too.
class PushService {
  PushService(this._api);

  final ApiClient _api;
  String? _token;
  final _subscriptions = <StreamSubscription<Object?>>[];

  static bool get available => Firebase.apps.isNotEmpty;

  /// Asks permission (Android 13+ shows a prompt once), registers the token, and
  /// starts listening for taps and token changes.
  Future<void> start(GoRouter router) async {
    if (!available) return;
    final messaging = FirebaseMessaging.instance;
    try {
      final settings = await messaging.requestPermission();
      if (settings.authorizationStatus == AuthorizationStatus.denied) return;
      final token = await messaging.getToken();
      if (token != null) await _register(token);
    } catch (error) {
      // No Play services, no network: pushes simply do not arrive this session.
      debugPrint('push_start_failed: $error');
      return;
    }

    _subscriptions
      ..add(messaging.onTokenRefresh.listen(_register))
      ..add(FirebaseMessaging.onMessageOpenedApp.listen((message) => _open(router, message.data)));
    final launched = await messaging.getInitialMessage();
    if (launched != null) _open(router, launched.data);
  }

  /// Before signing out, while the session can still ask the API.
  Future<void> stop() async {
    for (final subscription in _subscriptions) {
      await subscription.cancel();
    }
    _subscriptions.clear();
    final token = _token;
    _token = null;
    if (!available || token == null) return;
    try {
      await _api.post('/notifications/devices/remove', body: {'token': token});
    } on ApiException {
      // The API forgets tokens that FCM reports dead; this is a courtesy.
    }
    try {
      await FirebaseMessaging.instance.deleteToken();
    } catch (_) {
      // Nothing more to do.
    }
  }

  Future<void> _register(String token) async {
    _token = token;
    try {
      await _api.post('/notifications/devices', body: {
        'token': token,
        'platform': Platform.isIOS ? 'ios' : 'android',
      });
    } on ApiException catch (error) {
      debugPrint('push_register_failed: ${error.code}');
    }
  }

  /// Where a notification leads, from the IDs the API puts in its data.
  static String? routeFor(Map<String, dynamic> data) {
    final conversation = data['conversation_id'] as String?;
    if (conversation != null) return Routes.conversation(conversation);
    final targetType = data['target_type'] as String?;
    final targetId = data['target_id'] as String?;
    if (targetType == 'post' && targetId != null) return Routes.post(targetId);
    final user = data['user_id'] as String?;
    if (data['type'] == 'circle' && user != null) return Routes.person(user);
    return null;
  }

  void _open(GoRouter router, Map<String, dynamic> data) {
    final route = routeFor(data);
    if (route != null) router.go(route);
  }
}

final pushServiceProvider = Provider<PushService>((ref) => PushService(ref.watch(apiClientProvider)));
