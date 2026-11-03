import '../../core/api/api_client.dart';
import '../../core/location.dart';
import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// Who wrote a post, as a visitor without an account sees it: nobody in
/// particular (decision M.9).
const anonymousNeighbour = Author(id: '', displayName: 'A neighbour', isVerifiedNeighbour: false);

/// One page of the feed for a visitor, and where they are.
class GuestFeed {
  const GuestFeed({required this.posts, required this.nextCursor, required this.area, required this.radiusKm});

  final List<Post> posts;
  final String? nextCursor;
  final AreaLabel area;
  final double radiusKm;
}

/// The feed before signing up. The reading goes in the body and is forgotten by
/// the server once it has found the area.
class GuestRepository {
  GuestRepository(this._api);

  final ApiClient _api;

  Future<GuestFeed> feed(Reading reading, {String? cursor}) async {
    final data = Map<String, dynamic>.from(
      await _api.post('/guest/feed', body: {...reading.toJson(), 'cursor': ?cursor}) as Map,
    );
    return GuestFeed(
      posts: [
        for (final item in data['items'] as List)
          Post.fromJson({
            ...Map<String, dynamic>.from(item as Map),
            'author': {
              'id': anonymousNeighbour.id,
              'display_name': anonymousNeighbour.displayName,
              'is_verified_neighbour': false,
            },
            'my_reaction': null,
            'status': 'visible',
          }),
      ],
      nextCursor: data['next_cursor'] as String?,
      area: AreaLabel.fromJson(Map<String, dynamic>.from(data['area_label'] as Map)),
      radiusKm: (data['radius_km'] as num).toDouble(),
    );
  }
}
