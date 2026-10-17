import '../auth/auth_models.dart';

/// Someone who wrote something: a name and a badge, nothing more.
class Author {
  const Author({required this.id, required this.displayName, required this.isVerifiedNeighbour});

  factory Author.fromJson(Map<String, dynamic> json) => Author(
        id: json['id'] as String,
        displayName: json['display_name'] as String,
        isVerifiedNeighbour: json['is_verified_neighbour'] as bool? ?? false,
      );

  final String id;
  final String displayName;
  final bool isVerifiedNeighbour;

  String get initials => displayName
      .split(RegExp(r'\s+'))
      .where((part) => part.isNotEmpty)
      .take(2)
      .map((part) => part[0].toUpperCase())
      .join();
}

/// One image, as its served sizes keyed by width.
class PostImage {
  const PostImage({required this.mediaId, required this.urlsByWidth});

  factory PostImage.fromJson(Map<String, dynamic> json) {
    final variants = Map<String, dynamic>.from(json['variants'] as Map);
    return PostImage(
      mediaId: json['media_id'] as String,
      urlsByWidth: {
        for (final entry in variants.entries)
          int.parse(entry.key): (entry.value as Map)['url'] as String,
      },
    );
  }

  final String mediaId;
  final Map<int, String> urlsByWidth;

  /// The smallest size at least [width] wide, else the largest there is: no
  /// more data than the screen needs.
  String urlFor(int width) {
    final sizes = urlsByWidth.keys.toList()..sort();
    for (final size in sizes) {
      if (size >= width) return urlsByWidth[size]!;
    }
    return urlsByWidth[sizes.last]!;
  }
}

/// How far away, as the API's bands. There is never a number.
String? distanceLabel(String? bucket) => switch (bucket) {
      'under_1km' => 'under 1 km',
      '1_3km' => '1–3 km',
      '3_10km' => '3–10 km',
      '10km_plus' => '10+ km',
      _ => null,
    };

const postCategories = <String, String>{
  'general': 'General',
  'question': 'Question',
  'recommendation': 'Recommendation',
  'lost_and_found': 'Lost & found',
  'alert': 'Alert',
};

class Post {
  const Post({
    required this.id,
    required this.author,
    required this.body,
    required this.category,
    required this.images,
    required this.areaLabel,
    required this.distanceBucket,
    required this.commentCount,
    required this.reactionCount,
    required this.myReaction,
    required this.status,
    required this.createdAt,
  });

  factory Post.fromJson(Map<String, dynamic> json) => Post(
        id: json['id'] as String,
        author: Author.fromJson(Map<String, dynamic>.from(json['author'] as Map)),
        body: json['body'] as String,
        category: json['category'] as String,
        images: [
          for (final image in json['images'] as List)
            PostImage.fromJson(Map<String, dynamic>.from(image as Map)),
        ],
        areaLabel: AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
        distanceBucket: json['distance_bucket'] as String?,
        commentCount: json['comment_count'] as int,
        reactionCount: json['reaction_count'] as int,
        myReaction: json['my_reaction'] as String?,
        status: json['status'] as String,
        createdAt: DateTime.parse(json['created_at'] as String),
      );

  final String id;
  final Author author;
  final String body;
  final String category;
  final List<PostImage> images;
  final AreaLabel areaLabel;
  final String? distanceBucket;
  final int commentCount;
  final int reactionCount;
  final String? myReaction;
  final String status;
  final DateTime createdAt;

  bool get isHeld => status == 'hidden_pending_review';
  String get categoryLabel => postCategories[category] ?? category;

  Post copyWith({int? commentCount, int? reactionCount, String? myReaction, bool clearReaction = false}) {
    return Post(
      id: id,
      author: author,
      body: body,
      category: category,
      images: images,
      areaLabel: areaLabel,
      distanceBucket: distanceBucket,
      commentCount: commentCount ?? this.commentCount,
      reactionCount: reactionCount ?? this.reactionCount,
      myReaction: clearReaction ? null : (myReaction ?? this.myReaction),
      status: status,
      createdAt: createdAt,
    );
  }
}

class Comment {
  const Comment({
    required this.id,
    required this.author,
    required this.body,
    required this.isVisiting,
    required this.createdAt,
    this.authorArea,
  });

  factory Comment.fromJson(Map<String, dynamic> json) => Comment(
        id: json['id'] as String,
        author: Author.fromJson(Map<String, dynamic>.from(json['author'] as Map)),
        body: json['body'] as String,
        isVisiting: json['is_visiting'] as bool? ?? false,
        createdAt: DateTime.parse(json['created_at'] as String),
        authorArea: json['author_area_label'] == null
            ? null
            : AreaLabel.fromJson(Map<String, dynamic>.from(json['author_area_label'] as Map)),
      );

  final String id;
  final Author author;
  final String body;
  final bool isVisiting;
  final DateTime createdAt;
  final AreaLabel? authorArea;
}

/// A page of anything, as every list in the API returns it.
class ResultPage<T> {
  const ResultPage(this.items, this.nextCursor);
  final List<T> items;
  final String? nextCursor;
}

class FeedPage extends ResultPage<Post> {
  const FeedPage(super.items, super.nextCursor, {required this.radiusKm});
  final double radiusKm;
}

/// "20 min", "3 h", "Tue", "12 Oct": how long ago, briefly.
String timeAgo(DateTime moment, {DateTime? now}) {
  final current = now ?? DateTime.now();
  final elapsed = current.difference(moment);
  if (elapsed.inMinutes < 1) return 'now';
  if (elapsed.inMinutes < 60) return '${elapsed.inMinutes} min';
  if (elapsed.inHours < 24) return '${elapsed.inHours} h';
  if (elapsed.inDays < 7) {
    const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
    return days[moment.toLocal().weekday - 1];
  }
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  final local = moment.toLocal();
  return '${local.day} ${months[local.month - 1]}';
}
