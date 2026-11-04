import '../../core/api/api_client.dart';
import '../feed/feed_models.dart';
import 'story_models.dart';

/// Someone who watched my story, and when.
class StoryViewer {
  const StoryViewer(this.user, this.viewedAt);

  factory StoryViewer.fromJson(Map<String, dynamic> json) => StoryViewer(
    Author.fromJson(Map<String, dynamic>.from(json['user'] as Map)),
    DateTime.parse(json['viewed_at'] as String),
  );

  final Author user;
  final DateTime viewedAt;
}

/// Stories nearby: the list, posting, watching, and my own stories' viewers.
class StoryRepository {
  StoryRepository(this._api);

  final ApiClient _api;

  /// Live stories within my feed radius, newest first. A day's worth is small, so
  /// the strip reads a few pages at most.
  Future<List<Story>> nearby({int maxPages = 5}) async {
    final stories = <Story>[];
    String? cursor;
    for (var page = 0; page < maxPages; page++) {
      final data = Map<String, dynamic>.from(
        await _api.get('/stories', query: {'cursor': ?cursor, 'limit': 50}) as Map,
      );
      stories.addAll([
        for (final item in data['items'] as List) Story.fromJson(Map<String, dynamic>.from(item as Map)),
      ]);
      cursor = data['next_cursor'] as String?;
      if (cursor == null) break;
    }
    return stories;
  }

  Future<Story> postText(String text) async => Story.fromJson(
    Map<String, dynamic>.from(
      await _api.post('/stories', body: {'kind': 'text', 'text': text.trim()}, idempotent: true) as Map,
    ),
  );

  Future<Story> postImage(String mediaId) async => Story.fromJson(
    Map<String, dynamic>.from(
      await _api.post('/stories', body: {'kind': 'image', 'media_id': mediaId}, idempotent: true) as Map,
    ),
  );

  /// Marks a story watched. Harmless to repeat; the author is never counted.
  Future<void> markSeen(String storyId) => _api.post('/stories/$storyId/view');

  Future<List<StoryViewer>> viewers(String storyId) async {
    final data = Map<String, dynamic>.from(await _api.get('/stories/$storyId/viewers') as Map);
    return [for (final item in data['items'] as List) StoryViewer.fromJson(Map<String, dynamic>.from(item as Map))];
  }

  Future<void> delete(String storyId) => _api.delete('/stories/$storyId');
}
