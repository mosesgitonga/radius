import '../../core/api/api_client.dart';
import 'review_models.dart';

/// Reviews: someone's rating and what people said, writing one, and replying.
class ReviewRepository {
  ReviewRepository(this._api);

  final ApiClient _api;

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

  /// [contextType] is `service_post` or `listing`; [contextId] is its ID.
  Future<Review> review({
    required String subjectId,
    required String contextType,
    required String contextId,
    required int rating,
    String? body,
  }) async =>
      Review.fromJson(Map<String, dynamic>.from(await _api.post('/reviews', body: {
        'subject_id': subjectId,
        'context_type': contextType,
        'context_id': contextId,
        'rating': rating,
        if (body != null && body.trim().isNotEmpty) 'body': body.trim(),
      }) as Map));

  Future<Review> reply(String reviewId, String body) async => Review.fromJson(
      Map<String, dynamic>.from(await _api.put('/reviews/$reviewId/reply', body: {'body': body.trim()}) as Map));
}
