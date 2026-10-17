import 'dart:async';
import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../core/api/api_client.dart';
import '../../core/api/api_exception.dart';
import 'feed_models.dart';

/// The feed, posts, comments, reactions, reports and photo uploads.
class FeedRepository {
  FeedRepository(this._api, {Dio? uploader}) : _uploader = uploader ?? Dio();

  final ApiClient _api;

  /// Talks to object storage directly, by the signed link the API issues:
  /// never sends the API's token there.
  final Dio _uploader;

  Future<FeedPage> feed({String? cursor}) async {
    final data = Map<String, dynamic>.from(
        await _api.get('/feed', query: {'cursor': ?cursor}) as Map);
    return FeedPage(
      [for (final item in data['items'] as List) Post.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
      radiusKm: (data['radius_km'] as num).toDouble(),
    );
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
    return (
      Post.fromJson(Map<String, dynamic>.from(data['post'] as Map)),
      data['reach_estimate'] as int,
    );
  }

  /// Uploads one photo and waits until the server has processed it (resized,
  /// location data stripped). Returns its media ID.
  Future<String> uploadPhoto(
    Uint8List bytes, {
    String contentType = 'image/jpeg',
    String purpose = 'post',
    Duration timeout = const Duration(seconds: 45),
  }) async {
    final ticket = Map<String, dynamic>.from(await _api.post('/media/uploads',
        body: {'purpose': purpose, 'content_type': contentType}) as Map);
    final mediaId = ticket['media_id'] as String;
    final maxBytes = ticket['max_bytes'] as int;
    if (bytes.length > maxBytes) {
      throw ApiException(
        code: 'too_large',
        message: 'That photo is too big. Choose one under ${maxBytes ~/ (1024 * 1024)} MB.',
      );
    }
    try {
      final response = await _uploader.request<dynamic>(
        ticket['upload_url'] as String,
        data: Stream.fromIterable([bytes]),
        options: Options(
          method: (ticket['upload_method'] as String?) ?? 'PUT',
          headers: {
            ...Map<String, dynamic>.from(ticket['upload_headers'] as Map),
            Headers.contentLengthHeader: bytes.length,
          },
          validateStatus: (_) => true,
        ),
      );
      if ((response.statusCode ?? 500) >= 300) {
        throw const ApiException(code: 'upload_failed', message: "The photo didn't upload. Try again.");
      }
    } on DioException {
      throw const ApiException.network();
    }
    await _api.post('/media/$mediaId/complete');

    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final media = Map<String, dynamic>.from(await _api.get('/media/$mediaId') as Map);
      switch (media['status']) {
        case 'ready':
          return mediaId;
        case 'rejected':
          throw ApiException(
            code: 'photo_rejected',
            message: (media['rejection_reason'] as String?) ?? "That photo can't be used.",
          );
      }
      await Future<void>.delayed(const Duration(milliseconds: 800));
    }
    throw const ApiException(code: 'photo_timeout', message: 'The photo is taking too long. Try again.');
  }
}
