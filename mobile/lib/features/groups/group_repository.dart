import '../../core/api/api_client.dart';
import '../feed/feed_models.dart';
import 'group_models.dart';

/// Groups: finding, joining, posting, and the admin's tools.
class GroupRepository {
  GroupRepository(this._api);

  final ApiClient _api;

  static Group _group(dynamic json) => Group.fromJson(Map<String, dynamic>.from(json as Map));

  static List<T> _items<T>(Map<String, dynamic> data, T Function(Map<String, dynamic>) read) =>
      [for (final item in data['items'] as List) read(Map<String, dynamic>.from(item as Map))];

  Future<ResultPage<Group>> mine({String? cursor}) async {
    final data = Map<String, dynamic>.from(await _api.get('/groups/mine', query: {'cursor': ?cursor}) as Map);
    return ResultPage(_items(data, Group.fromJson), data['next_cursor'] as String?);
  }

  Future<List<Group>> invitations() async =>
      [for (final item in await _api.get('/groups/invitations') as List) _group(item)];

  /// Public groups nearby: busiest first, or with [query] the best name matches.
  Future<GroupDiscovery> discover({String? query, String? category, String? cursor}) async {
    final q = query?.trim();
    final data = Map<String, dynamic>.from(await _api.get('/groups/discover', query: {
      if (q != null && q.isNotEmpty) 'q': q,
      'category': ?category,
      'cursor': ?cursor,
    }) as Map);
    return GroupDiscovery(
      _items(data, Group.fromJson),
      data['next_cursor'] as String?,
      radiusKm: (data['radius_km'] as num).toDouble(),
    );
  }

  Future<Group> group(String id) async => _group(await _api.get('/groups/$id'));

  /// The fields a group is made or changed with. On an edit, only what is sent
  /// changes.
  static Map<String, dynamic> fields({
    required String name,
    required String description,
    required String category,
    required bool isPublic,
    required bool needsApproval,
    required bool approvesPosts,
    String? rules,
    String? coverMediaId,
  }) =>
      {
        'name': name,
        'description': description,
        'category': category,
        'visibility': isPublic ? 'public' : 'private',
        'join_policy': needsApproval ? 'approval' : 'open',
        'requires_post_approval': approvesPosts,
        'rules': rules,
        'cover_media_id': ?coverMediaId,
      };

  Future<Group> create(Map<String, dynamic> fields) async => _group(await _api.post('/groups', body: fields));

  Future<Group> update(String id, Map<String, dynamic> fields) async =>
      _group(await _api.patch('/groups/$id', body: fields));

  /// Joins an open group at once, or asks to join one that approves members.
  Future<Group> join(String id) async => _group(await _api.post('/groups/$id/join'));

  /// Leaves, or takes back a request to join.
  Future<void> leave(String id) => _api.post('/groups/$id/leave');

  Future<Group> answerInvitation(String id, {required bool accept}) async =>
      _group(await _api.post('/groups/$id/invitation', body: {'action': accept ? 'accept' : 'decline'}));

  Future<Group> previewLink(String token) async => _group(await _api.get('/groups/invite-links/$token'));

  Future<Group> joinWithLink(String token) async => _group(await _api.post('/groups/invite-links/$token/join'));

  /// A new invite link's token. It is shown once: only its hash is kept.
  Future<String> createInviteLink(String id) async {
    final data = Map<String, dynamic>.from(await _api.post('/groups/$id/invite-links', body: {}) as Map);
    return data['token'] as String;
  }

  Future<Group> enableChat(String id) async => _group(await _api.post('/groups/$id/chat'));

  Future<GroupPostPage> posts(String id, {String? cursor}) async {
    final data = Map<String, dynamic>.from(await _api.get('/groups/$id/posts', query: {'cursor': ?cursor}) as Map);
    return GroupPostPage(
      _items(data, Post.fromJson),
      data['next_cursor'] as String?,
      pinned: [for (final item in data['pinned'] as List? ?? const []) Post.fromJson(Map<String, dynamic>.from(item as Map))],
    );
  }

  Future<Post> createPost(String id, {required String body, required String category, List<String> mediaIds = const []}) async =>
      Post.fromJson(Map<String, dynamic>.from(await _api.post('/groups/$id/posts',
          body: {'body': body, 'category': category, 'media_ids': mediaIds}, idempotent: true) as Map));

  Future<ResultPage<Post>> pendingPosts(String id, {String? cursor}) async {
    final data =
        Map<String, dynamic>.from(await _api.get('/groups/$id/posts/pending', query: {'cursor': ?cursor}) as Map);
    return ResultPage(_items(data, Post.fromJson), data['next_cursor'] as String?);
  }

  Future<void> reviewPost(String id, String postId, {required bool approve}) =>
      _api.post('/groups/$id/posts/$postId/review', body: {'action': approve ? 'approve' : 'reject'});

  Future<void> pin(String id, String postId, {required bool pinned}) =>
      _api.put('/groups/$id/posts/$postId/pin', body: {'pinned': pinned});

  Future<void> removePost(String id, String postId) => _api.delete('/groups/$id/posts/$postId');

  /// Members, or for admins `pending` requests.
  Future<ResultPage<GroupMember>> members(String id, {String status = 'active', String? cursor}) async {
    final data = Map<String, dynamic>.from(
        await _api.get('/groups/$id/members', query: {'status': status, 'cursor': ?cursor}) as Map);
    return ResultPage(_items(data, GroupMember.fromJson), data['next_cursor'] as String?);
  }

  Future<void> reviewRequest(String id, String userId, {required bool approve}) =>
      _api.post('/groups/$id/members/$userId/review', body: {'action': approve ? 'approve' : 'reject'});

  Future<void> removeMember(String id, String userId) => _api.delete('/groups/$id/members/$userId');
}
