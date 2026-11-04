import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// A story: a photo or a line of text, gone in a day.
class Story {
  const Story({
    required this.id,
    required this.author,
    required this.kind,
    required this.areaLabel,
    required this.createdAt,
    required this.expiresAt,
    this.image = const {},
    this.text,
    this.distanceBucket,
    this.viewCount,
    this.seen = false,
  });

  factory Story.fromJson(Map<String, dynamic> json) => Story(
    id: json['id'] as String,
    author: Author.fromJson(Map<String, dynamic>.from(json['author'] as Map)),
    kind: json['kind'] as String,
    image: imageSizes(json['image']),
    text: json['text'] as String?,
    areaLabel: AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
    distanceBucket: json['distance_bucket'] as String?,
    createdAt: DateTime.parse(json['created_at'] as String),
    expiresAt: DateTime.parse(json['expires_at'] as String),
    viewCount: json['view_count'] as int?,
    seen: json['seen'] as bool? ?? false,
  );

  final String id;
  final Author author;
  final String kind;
  final Map<int, String> image;
  final String? text;
  final AreaLabel areaLabel;
  final String? distanceBucket;
  final DateTime createdAt;
  final DateTime expiresAt;

  /// How many neighbours watched it; only ever set on my own.
  final int? viewCount;
  final bool seen;

  bool get isImage => kind == 'image';

  Story asSeen() => Story(
    id: id,
    author: author,
    kind: kind,
    areaLabel: areaLabel,
    createdAt: createdAt,
    expiresAt: expiresAt,
    image: image,
    text: text,
    distanceBucket: distanceBucket,
    viewCount: viewCount,
    seen: true,
  );
}

/// One person's live stories, oldest first, as the viewer plays them.
class StoryGroup {
  const StoryGroup(this.author, this.stories);

  final Author author;
  final List<Story> stories;

  bool get allSeen => stories.every((story) => story.seen);

  /// Where to start: the first story not yet watched, else the first.
  int get firstUnseen {
    final index = stories.indexWhere((story) => !story.seen);
    return index < 0 ? 0 : index;
  }
}

/// Groups a newest-first page by person: mine first, then people with something
/// unwatched, then the rest, each by their newest story.
List<StoryGroup> groupStories(List<Story> stories, {required String myId}) {
  final byAuthor = <String, List<Story>>{};
  final authors = <String, Author>{};
  for (final story in stories) {
    byAuthor.putIfAbsent(story.author.id, () => []).add(story);
    authors[story.author.id] = story.author;
  }
  final groups = [
    for (final entry in byAuthor.entries)
      StoryGroup(authors[entry.key]!, [...entry.value]..sort((a, b) => a.createdAt.compareTo(b.createdAt))),
  ];
  int rank(StoryGroup group) => group.author.id == myId ? 0 : (group.allSeen ? 2 : 1);
  groups.sort((a, b) {
    final byRank = rank(a).compareTo(rank(b));
    if (byRank != 0) return byRank;
    return b.stories.last.createdAt.compareTo(a.stories.last.createdAt);
  });
  return groups;
}

/// The background of a text story: chosen from its ID, so everyone sees the same.
const storyColours = [0xFFC4502D, 0xFF2E6B5A, 0xFF3B4A8C, 0xFF8A4F7D, 0xFF9A6A1F, 0xFF1F5F7A];

int storyColour(String id) => storyColours[id.codeUnits.fold<int>(0, (a, b) => a + b) % storyColours.length];
