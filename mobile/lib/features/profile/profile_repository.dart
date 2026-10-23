import '../../core/api/api_client.dart';
import '../auth/auth_models.dart';
import '../feed/feed_models.dart';
import '../market/listing_models.dart';
import '../services/service_models.dart';
import 'profile_models.dart';

/// Profiles: someone's details, their posts, what they sell and offer; and my
/// own bio, photo and cover.
class ProfileRepository {
  ProfileRepository(this._api);

  final ApiClient _api;

  Future<Profile> profile(String userId) async =>
      Profile.fromJson(Map<String, dynamic>.from(await _api.get('/users/$userId') as Map));

  /// Only what the viewer's feed could show them; see [Profile.postsVisible].
  Future<ResultPage<Post>> posts(String userId, {String? cursor}) async {
    final data = Map<String, dynamic>.from(
      await _api.get('/users/$userId/posts', query: {'cursor': ?cursor}) as Map,
    );
    return ResultPage([
      for (final item in data['items'] as List) Post.fromJson(Map<String, dynamic>.from(item as Map)),
    ], data['next_cursor'] as String?);
  }

  Future<ResultPage<Listing>> listings(String userId, {String? cursor}) async {
    final data = Map<String, dynamic>.from(
      await _api.get('/users/$userId/listings', query: {'cursor': ?cursor}) as Map,
    );
    return ResultPage([
      for (final item in data['items'] as List) Listing.fromJson(Map<String, dynamic>.from(item as Map)),
    ], data['next_cursor'] as String?);
  }

  Future<ResultPage<ServicePost>> services(String userId, {String? cursor}) async {
    final data = Map<String, dynamic>.from(
      await _api.get('/users/$userId/services', query: {'cursor': ?cursor}) as Map,
    );
    return ResultPage([
      for (final item in data['items'] as List) ServicePost.fromJson(Map<String, dynamic>.from(item as Map)),
    ], data['next_cursor'] as String?);
  }

  /// Changes the name and bio; an empty bio removes it.
  Future<Me> updateDetails({required String displayName, required String bio}) async => Me.fromJson(
    Map<String, dynamic>.from(
      await _api.patch('/me', body: {'display_name': displayName.trim(), 'bio': bio.trim()}) as Map,
    ),
  );

  /// [mediaId] is a ready `avatar` upload, or null to remove the photo.
  Future<void> setAvatar(String? mediaId) => _api.put('/me/avatar', body: {'media_id': mediaId});

  /// [mediaId] is a ready `cover` upload, or null to remove the cover.
  Future<void> setCover(String? mediaId) => _api.put('/me/cover', body: {'media_id': mediaId});

  Future<void> report(String userId, {required String reason, String? details}) => _api.post(
    '/reports',
    body: {
      'target_type': 'user',
      'target_id': userId,
      'reason': reason,
      if (details != null && details.trim().isNotEmpty) 'details': details.trim(),
    },
  );

  Future<void> block(String userId) => _api.post('/me/blocks', body: {'user_id': userId});
}
