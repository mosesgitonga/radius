import '../../core/api/api_client.dart';
import '../chats/chat_models.dart';
import '../feed/feed_models.dart';
import 'service_models.dart';

/// Services, needs and jobs, and the reviews people leave.
class ServiceRepository {
  ServiceRepository(this._api);

  final ApiClient _api;

  Future<ServicePage> browse({
    required ServiceKind kind,
    String? category,
    String? query,
    String? cursor,
  }) async {
    final q = query?.trim();
    final data = Map<String, dynamic>.from(await _api.get('/services', query: {
      'kind': kind.key,
      'category': ?category,
      if (q != null && q.isNotEmpty) 'q': q,
      'cursor': ?cursor,
    }) as Map);
    return ServicePage(
      [for (final item in data['items'] as List) ServicePost.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
      radiusKm: (data['radius_km'] as num).toDouble(),
    );
  }

  Future<ResultPage<ServicePost>> mine({String? cursor}) async {
    final data = Map<String, dynamic>.from(await _api.get('/services/mine', query: {'cursor': ?cursor}) as Map);
    return ResultPage(
      [for (final item in data['items'] as List) ServicePost.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
    );
  }

  Future<ServicePost> post(String id) async =>
      ServicePost.fromJson(Map<String, dynamic>.from(await _api.get('/services/$id') as Map));

  /// [fields] are the kind's own: the server refuses another kind's.
  Future<ServicePost> create(ServiceKind kind, Map<String, dynamic> fields) async => ServicePost.fromJson(
      Map<String, dynamic>.from(
          await _api.post('/services', body: {'kind': kind.key, ...fields}, idempotent: true) as Map));

  Future<ServicePost> update(String id, Map<String, dynamic> changes) async =>
      ServicePost.fromJson(Map<String, dynamic>.from(await _api.patch('/services/$id', body: changes) as Map));

  Future<ServicePost> setOpen(String id, bool open) async => ServicePost.fromJson(
      Map<String, dynamic>.from(await _api.put('/services/$id/open', body: {'open': open}) as Map));

  Future<void> delete(String id) => _api.delete('/services/$id');

  /// The conversation with the author about this post, made if need be.
  Future<Conversation> contact(String id) async =>
      Conversation.fromJson(Map<String, dynamic>.from(await _api.post('/services/$id/chat') as Map));

  Future<ReviewPage> reviewsOf(String userId, {String? cursor}) async {
    final data =
        Map<String, dynamic>.from(await _api.get('/users/$userId/reviews', query: {'cursor': ?cursor}) as Map);
    return ReviewPage(
      [for (final item in data['items'] as List) Review.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
      average: (data['rating_average'] as num?)?.toDouble(),
      count: data['rating_count'] as int? ?? 0,
    );
  }

  Future<Review> review({
    required String subjectId,
    required String postId,
    required int rating,
    String? body,
  }) async =>
      Review.fromJson(Map<String, dynamic>.from(await _api.post('/reviews', body: {
        'subject_id': subjectId,
        'context_type': 'service_post',
        'context_id': postId,
        'rating': rating,
        if (body != null && body.trim().isNotEmpty) 'body': body.trim(),
      }) as Map));

  Future<Review> reply(String reviewId, String body) async => Review.fromJson(
      Map<String, dynamic>.from(await _api.put('/reviews/$reviewId/reply', body: {'body': body.trim()}) as Map));
}
