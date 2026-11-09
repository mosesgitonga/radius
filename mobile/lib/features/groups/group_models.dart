import '../../core/config.dart';
import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// The server's group categories, with the names the app shows. The server's list
/// is configurable; anything it sends that isn't here is shown as it comes.
const groupCategories = {
  'estate': 'Estate',
  'church': 'Church',
  'chama': 'Chama',
  'students': 'Students',
  'sports': 'Sports',
  'business': 'Business',
  'other': 'Other',
};

/// Where the caller stands with a group.
enum GroupRole { owner, admin, moderator, member }

GroupRole _role(String value) => GroupRole.values.firstWhere((r) => r.name == value, orElse: () => GroupRole.member);

class Membership {
  const Membership({required this.role, required this.status});

  factory Membership.fromJson(Map<String, dynamic> json) =>
      Membership(role: _role(json['role'] as String), status: json['status'] as String);

  final GroupRole role;

  /// `active`, `pending` (asked to join), `invited` or `banned`.
  final String status;

  bool get isActive => status == 'active';
}

class Group {
  const Group({
    required this.id,
    required this.name,
    required this.description,
    required this.category,
    required this.isPublic,
    required this.needsApproval,
    required this.approvesPosts,
    required this.memberCount,
    this.rules,
    this.cover = const {},
    this.areaLabel,
    this.distanceBucket,
    this.membership,
    this.chatId,
  });

  factory Group.fromJson(Map<String, dynamic> json) => Group(
        id: json['id'] as String,
        name: json['name'] as String,
        description: json['description'] as String? ?? '',
        category: json['category'] as String,
        rules: json['rules'] as String?,
        isPublic: json['visibility'] == 'public',
        needsApproval: json['join_policy'] == 'approval',
        approvesPosts: json['requires_post_approval'] as bool? ?? false,
        memberCount: json['member_count'] as int? ?? 0,
        cover: imageSizes(json['cover']),
        areaLabel: json['area_label'] == null
            ? null
            : AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
        distanceBucket: json['distance_bucket'] as String?,
        membership: json['my_membership'] == null
            ? null
            : Membership.fromJson(Map<String, dynamic>.from(json['my_membership'] as Map)),
        chatId: json['chat_conversation_id'] as String?,
      );

  final String id;
  final String name;
  final String description;
  final String category;
  final String? rules;
  final bool isPublic;

  /// Joining waits for an admin.
  final bool needsApproval;

  /// Members' posts wait for a moderator.
  final bool approvesPosts;
  final int memberCount;
  final Map<int, String> cover;
  final AreaLabel? areaLabel;
  final String? distanceBucket;
  final Membership? membership;

  /// The linked group chat, shown to active members only.
  final String? chatId;

  String get categoryLabel => groupCategories[category] ?? category;
  bool get isMember => membership?.isActive ?? false;
  bool get isInvited => membership?.status == 'invited';
  bool get hasAsked => membership?.status == 'pending';
  GroupRole? get role => isMember ? membership!.role : null;

  /// Admins and the owner run the group; moderators look after its posts.
  bool get isAdmin => role == GroupRole.owner || role == GroupRole.admin;
  bool get isModerator => isAdmin || role == GroupRole.moderator;

  /// "Kamakwa · 1–3 km · 24 members"; a private group has no place.
  String get meta => [
        if (isPublic && areaLabel != null) areaLabel!.label,
        ?distanceLabel(distanceBucket),
        memberCount == 1 ? '1 member' : '$memberCount members',
      ].join(' · ');
}

class GroupMember {
  const GroupMember({required this.user, required this.role, required this.status});

  factory GroupMember.fromJson(Map<String, dynamic> json) => GroupMember(
        user: Author.fromJson(Map<String, dynamic>.from(json['user'] as Map)),
        role: _role(json['role'] as String),
        status: json['status'] as String,
      );

  final Author user;
  final GroupRole role;
  final String status;

  String? get roleLabel => switch (role) {
        GroupRole.owner => 'Owner',
        GroupRole.admin => 'Admin',
        GroupRole.moderator => 'Moderator',
        GroupRole.member => null,
      };
}

/// A page of a group's posts: pinned ones come on the first page only.
class GroupPostPage extends ResultPage<Post> {
  const GroupPostPage(super.items, super.nextCursor, {this.pinned = const []});
  final List<Post> pinned;
}

/// Public groups nearby, and how far the search reached.
class GroupDiscovery extends ResultPage<Group> {
  const GroupDiscovery(super.items, super.nextCursor, {required this.radiusKm});
  final double radiusKm;
}

/// An invite link's address, made from the app's server, and the token back out
/// of whatever someone pasted: the whole link, or just the code.
String inviteLinkFor(String token) => '${AppConfig.apiBaseUrl}/g/$token';

String? inviteTokenFrom(String pasted) {
  final text = pasted.trim();
  if (text.isEmpty) return null;
  final match = RegExp(r'/g/([A-Za-z0-9_\-]+)').firstMatch(text);
  if (match != null) return match.group(1);
  return RegExp(r'^[A-Za-z0-9_\-]{8,}$').hasMatch(text) ? text : null;
}

/// What the composer says once a group post is sent.
String groupPostPostedMessage(Post post) => post.awaitsApproval
    ? "Posted. It will appear in the group once a moderator approves it."
    : post.isHeld
        ? 'Posted. It will appear once a moderator has looked at it.'
        : 'Posted to the group.';
