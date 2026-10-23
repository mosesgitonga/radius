import 'dart:convert';

import '../../core/api/api_client.dart';
import '../auth/auth_models.dart';
import 'account_models.dart';

/// The account's own settings and rights: profile, standing, consents, blocks,
/// devices, notifications, the data export, and deletion.
class AccountRepository {
  AccountRepository(this._api);

  final ApiClient _api;

  Future<Me> rename(String displayName) async => Me.fromJson(
      Map<String, dynamic>.from(await _api.patch('/me', body: {'display_name': displayName.trim()}) as Map));

  Future<Standing> standing() async =>
      Standing.fromJson(Map<String, dynamic>.from(await _api.get('/me/standing') as Map));

  Future<List<Consent>> consents() async => [
        for (final item in await _api.get('/me/consents') as List)
          Consent.fromJson(Map<String, dynamic>.from(item as Map)),
      ];

  Future<Consent> setMarketing(bool granted) async => Consent.fromJson(Map<String, dynamic>.from(
      await _api.put('/me/consents', body: {'consent_type': 'marketing', 'granted': granted}) as Map));

  Future<List<BlockedPerson>> blocks() async => [
        for (final item in await _api.get('/me/blocks') as List)
          BlockedPerson.fromJson(Map<String, dynamic>.from(item as Map)),
      ];

  Future<void> unblock(String userId) => _api.delete('/me/blocks/$userId');

  Future<List<DeviceSession>> sessions() async => [
        for (final item in await _api.get('/auth/sessions') as List)
          DeviceSession.fromJson(Map<String, dynamic>.from(item as Map)),
      ];

  /// Signs out every other device; this one stays signed in. Returns how many.
  Future<int> signOutOthers() async {
    final data = Map<String, dynamic>.from(
        await _api.post('/auth/logout-all', body: {'keep_current_session': true}) as Map);
    return data['sessions_revoked'] as int? ?? 0;
  }

  Future<List<NotificationPreference>> preferences() async => [
        for (final item in await _api.get('/notifications/preferences') as List)
          NotificationPreference.fromJson(Map<String, dynamic>.from(item as Map)),
      ];

  Future<NotificationPreference> setPreference(NotificationPreference preference) async =>
      NotificationPreference.fromJson(Map<String, dynamic>.from(
          await _api.put('/notifications/preferences/${preference.type}', body: {
        'push_enabled': preference.push,
        'in_app_enabled': preference.inApp,
        'show_preview': preference.preview,
      }) as Map));

  /// Everything held about the account, as readable JSON.
  Future<String> export() async =>
      const JsonEncoder.withIndent('  ').convert(await _api.get('/me/export'));

  /// Starts the grace period. The server signs out every device at once.
  Future<DeletionStatus> requestDeletion() async =>
      DeletionStatus.fromJson(Map<String, dynamic>.from(await _api.post('/me/deletion-request') as Map));

  Future<void> cancelDeletion() => _api.delete('/me/deletion-request');
}
