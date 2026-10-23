import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// Someone's profile, as `GET /v1/users/{id}` gives it: an area label and a
/// distance band, never a position.
class Profile {
  const Profile({
    required this.id,
    required this.displayName,
    required this.isVerifiedNeighbour,
    required this.ratingCount,
    required this.postsVisible,
    this.avatar = const {},
    this.cover = const {},
    this.bio,
    this.areaLabel,
    this.distanceBucket,
    this.memberSince,
    this.ratingAverage,
  });

  factory Profile.fromJson(Map<String, dynamic> json) => Profile(
    id: json['id'] as String,
    displayName: json['display_name'] as String,
    isVerifiedNeighbour: json['is_verified_neighbour'] as bool? ?? false,
    ratingAverage: (json['rating_average'] as num?)?.toDouble(),
    ratingCount: json['rating_count'] as int? ?? 0,
    postsVisible: json['posts_visible'] as bool? ?? false,
    avatar: imageSizes(json['avatar']),
    cover: imageSizes(json['cover']),
    bio: json['bio'] as String?,
    areaLabel: json['area_label'] == null
        ? null
        : AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
    distanceBucket: json['distance_bucket'] as String?,
    memberSince: json['member_since'] == null ? null : DateTime.parse(json['member_since'] as String),
  );

  final String id;
  final String displayName;
  final bool isVerifiedNeighbour;
  final double? ratingAverage;
  final int ratingCount;

  /// Whether the viewer's feed reaches this person's area, so whether their
  /// posts can be listed here.
  final bool postsVisible;
  final Map<int, String> avatar;
  final Map<int, String> cover;
  final String? bio;
  final AreaLabel? areaLabel;
  final String? distanceBucket;
  final DateTime? memberSince;

  Author get asAuthor =>
      Author(id: id, displayName: displayName, isVerifiedNeighbour: isVerifiedNeighbour, avatar: avatar);

  /// "Nyeri Town · 1–3 km".
  String get where => [?areaLabel?.label, ?distanceLabel(distanceBucket)].join(' · ');
}
