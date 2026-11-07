import 'dart:async';

import '../../core/api/api_client.dart';
import '../../core/offline_store.dart';
import 'feed_models.dart';

/// The feed, posts, comments, reactions and reports.
class FeedRepository {
  FeedRepository(this._api, [OfflineStore? offline]) : _offline = offline ?? MemoryOfflineStore();

  final ApiClient _api;
  final OfflineStore _offline;

  /// Kept offline: the newest [savedPosts] posts of the Home feed.
  static const savedPosts = 50;

  Future<FeedPage> feed({String? cursor}) async {
    final Map<String, dynamic> data;
    if (cursor == null) {
      data = Map<String, dynamic>.from(await fetchOrRecall(
        _offline,
        'feed',
        () => _api.get('/feed'),
        keep: (json) => {...(json as Map), 'next_cursor': null},
      ) as Map);
    } else {
      data = Map<String, dynamic>.from(await _api.get('/feed', query: {'cursor': cursor}) as Map);
      unawaited(_saveMore(data['items'] as List));
    }
    return FeedPage(
      [for (final item in data['items'] as List) Post.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
      radiusKm: (data['radius_km'] as num).toDouble(),
    );
  }

  /// Later pages join the saved first page, up to [savedPosts].
  Future<void> _saveMore(List<dynamic> items) async {
    final saved = await _offline.read('feed');
    if (saved is! Map) return;
    final have = List<dynamic>.from(saved['items'] as List);
    if (have.length >= savedPosts) return;
    final known = {for (final item in have) (item as Map)['id']};
    have.addAll(items.where((item) => !known.contains((item as Map)['id'])));
    await _offline.write('feed', {...saved, 'items': have.take(savedPosts).toList()});
  }

  Future<void> _savePosted(Object? post) async {
    final saved = await _offline.read('feed');
    if (saved is! Map || post is! Map) return;
    final items = [post, ...(saved['items'] as List).where((item) => (item as Map)['id'] != post['id'])];
    await _offline.write('feed', {...saved, 'items': items.take(savedPosts).toList()});
  }

  Future<Post> post(String id) async =>
      Post.fromJson(Map<String, dynamic>.from(await _api.get('/posts/$id') as Map));

  Future<ResultPage<Comment>> comments(String postId, {String? cursor}) async {
    final data = Map<String, dynamic>.from(
        await _api.get('/posts/$postId/comments', query: {'cursor': ?cursor}) as Map);
    return ResultPage(
      [for (final item in data['items'] as List) Comment.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
    );
  }

  Future<Comment> addComment(String postId, String body) async => Comment.fromJson(
      Map<String, dynamic>.from(await _api.post('/posts/$postId/comments',
          body: {'body': body}, idempotent: true) as Map));

  /// Sets the caller's reaction; returns the new count.
  Future<int> react(String postId, String kind) async {
    final data = Map<String, dynamic>.from(
        await _api.put('/posts/$postId/reaction', body: {'kind': kind}) as Map);
    return data['reaction_count'] as int;
  }

  /// Removes it. The server answers with no body, so the count is the caller's
  /// to adjust.
  Future<void> unreact(String postId) => _api.delete('/posts/$postId/reaction');

  Future<void> report({
    required String targetType,
    required String targetId,
    required String reason,
    String? details,
  }) =>
      _api.post('/reports', body: {
        'target_type': targetType,
        'target_id': targetId,
        'reason': reason,
        if (details != null && details.trim().isNotEmpty) 'details': details.trim(),
      });

  /// Posts to the caller's home area. Returns the post and roughly how many
  /// neighbours can see it.
  Future<(Post, int)> createPost({
    required String body,
    required String category,
    List<String> mediaIds = const [],
  }) async {
    final data = Map<String, dynamic>.from(await _api.post('/posts',
        body: {'body': body, 'category': category, 'media_ids': mediaIds}, idempotent: true) as Map);
    // Shown at the top of the feed straight away, so it joins the saved copy too.
    unawaited(_savePosted(data['post']));
    return (
      Post.fromJson(Map<String, dynamic>.from(data['post'] as Map)),
      data['reach_estimate'] as int,
    );
  }
}
