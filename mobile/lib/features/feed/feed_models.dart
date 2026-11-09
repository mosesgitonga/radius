import '../auth/auth_models.dart';

/// Image sizes as the API sends them, `{"320": {"url": ...}}`, keyed by width.
Map<int, String> imageSizes(Object? json) => json is Map
    ? {for (final entry in json.entries) int.parse('${entry.key}'): (entry.value as Map)['url'] as String}
    : const {};

/// The smallest size at least [width] wide, else the largest there is.
String? sizeFor(Map<int, String> sizes, int width) {
  if (sizes.isEmpty) return null;
  final widths = sizes.keys.toList()..sort();
  for (final size in widths) {
    if (size >= width) return sizes[size];
  }
  return sizes[widths.last];
}

/// Someone who wrote something: a name, a badge and a photo, nothing more.
class Author {
  const Author({
    required this.id,
    required this.displayName,
    required this.isVerifiedNeighbour,
    this.avatar = const {},
  });

  factory Author.fromJson(Map<String, dynamic> json) => Author(
        id: json['id'] as String,
        displayName: json['display_name'] as String,
        isVerifiedNeighbour: json['is_verified_neighbour'] as bool? ?? false,
        avatar: imageSizes(json['avatar']),
      );

  final String id;
  final String displayName;
  final bool isVerifiedNeighbour;

  /// The profile photo's sizes, keyed by width; empty when there is none.
  final Map<int, String> avatar;

  String get initials => displayName
      .split(RegExp(r'\s+'))
      .map((part) => part.replaceAll(RegExp(r'[^\p{L}\p{N}]', unicode: true), ''))
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
    this.groupId,
    this.approvalStatus,
    this.pinned = false,
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
        groupId: json['group_id'] as String?,
        approvalStatus: json['approval_status'] as String?,
        pinned: json['pinned'] as bool? ?? false,
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

  /// Set for a post in a group; such posts never reach the neighbourhood feed.
  final String? groupId;

  /// In a group that approves posts: `pending`, `approved` or `rejected`.
  final String? approvalStatus;
  final bool pinned;

  bool get isHeld => status == 'hidden_pending_review';
  bool get awaitsApproval => approvalStatus == 'pending';
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
      groupId: groupId,
      approvalStatus: approvalStatus,
      pinned: pinned,
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
    this.parentId,
    this.replyCount = 0,
    this.score = 0,
    this.myVote = 0,
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
        parentId: json['parent_id'] as String?,
        replyCount: json['reply_count'] as int? ?? 0,
        score: json['score'] as int? ?? 0,
        myVote: json['my_vote'] as int? ?? 0,
      );

  final String id;
  final Author author;
  final String body;
  final bool isVisiting;
  final DateTime createdAt;
  final AreaLabel? authorArea;

  /// The top-level comment this replies to; null for a top-level comment.
  final String? parentId;
  final int replyCount;

  /// Upvotes minus downvotes. Top-level comments come highest first.
  final int score;

  /// The viewer's vote: 1 up, -1 down, 0 none.
  final int myVote;

  /// The top-level comment whose thread this belongs to. Replies are one level
  /// deep, so a reply to a reply joins the same thread.
  String get threadId => parentId ?? id;

  Comment copyWith({int? replyCount, int? score, int? myVote}) => Comment(
        id: id,
        author: author,
        body: body,
        isVisiting: isVisiting,
        createdAt: createdAt,
        authorArea: authorArea,
        parentId: parentId,
        replyCount: replyCount ?? this.replyCount,
        score: score ?? this.score,
        myVote: myVote ?? this.myVote,
      );
}

/// A comment's score and the viewer's vote, as the server has them after a vote.
typedef VoteResult = ({int score, int myVote});

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
