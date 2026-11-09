import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../auth/auth_controller.dart';
import '../../feed/feed_models.dart';
import '../../feed/widgets/post_card.dart';
import '../../profile/profile_screen.dart';
import '../group_models.dart';

const _rank = {GroupRole.owner: 3, GroupRole.admin: 2, GroupRole.moderator: 1, GroupRole.member: 0};

/// A group's members, and for its admins the people asking to join.
class GroupMembersScreen extends ConsumerStatefulWidget {
  const GroupMembersScreen({super.key, required this.groupId});

  final String groupId;

  @override
  ConsumerState<GroupMembersScreen> createState() => _GroupMembersScreenState();
}

class _GroupMembersScreenState extends ConsumerState<GroupMembersScreen> {
  Group? _group;
  List<GroupMember> _members = const [];
  List<GroupMember> _requests = const [];
  String? _cursor;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final repo = ref.read(groupRepositoryProvider);
    try {
      final group = await repo.group(widget.groupId);
      final members = await repo.members(widget.groupId);
      final requests = group.isAdmin ? (await repo.members(widget.groupId, status: 'pending')).items : <GroupMember>[];
      if (!mounted) return;
      setState(() {
        _group = group;
        _members = members.items;
        _cursor = members.nextCursor;
        _requests = requests;
        _loading = false;
        _error = null;
      });
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = error.message;
        });
      }
    }
  }

  Future<void> _more() async {
    if (_cursor == null) return;
    try {
      final page = await ref.read(groupRepositoryProvider).members(widget.groupId, cursor: _cursor);
      if (mounted) {
        setState(() {
          _members = [..._members, ...page.items];
          _cursor = page.nextCursor;
        });
      }
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _review(GroupMember person, {required bool approve}) async {
    try {
      await ref.read(groupRepositoryProvider).reviewRequest(widget.groupId, person.user.id, approve: approve);
      _toast(approve ? '${person.user.displayName} is in.' : 'Request declined.');
      await _load();
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _remove(GroupMember person) async {
    final sure = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: Text('Remove ${person.user.displayName}?'),
        content: const Text('They leave the group and its chat. They can join again later.'),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.of(dialog).pop(true), child: const Text('Remove')),
        ],
      ),
    );
    if (sure != true) return;
    try {
      await ref.read(groupRepositoryProvider).removeMember(widget.groupId, person.user.id);
      _toast('${person.user.displayName} was removed.');
      await _load();
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final group = _group;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final myRank = group?.role == null ? -1 : _rank[group!.role]!;

    Widget person(GroupMember member, {Widget? trailing}) => ListTile(
          contentPadding: const EdgeInsets.symmetric(horizontal: 4),
          leading: GestureDetector(
            onTap: () => openProfile(context, ref, member.user.id),
            child: InitialsAvatar(author: member.user),
          ),
          title: Text(member.user.displayName),
          subtitle: member.roleLabel == null ? null : Text(member.roleLabel!),
          trailing: trailing,
          onTap: () => openProfile(context, ref, member.user.id),
        );

    return Scaffold(
      appBar: AppBar(title: Text(group == null ? 'Members' : 'Members of ${group.name}', overflow: TextOverflow.ellipsis)),
      body: _loading
          ? Center(child: CircularProgressIndicator(color: Palette.primary))
          : RefreshIndicator(
              color: Palette.primary,
              onRefresh: _load,
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 32),
                children: [
                  if (_error != null) Text(_error!, style: text.bodyMedium),
                  if (_requests.isNotEmpty) ...[
                    Text('Asking to join', style: text.titleMedium),
                    for (final request in _requests)
                      person(
                        request,
                        trailing: Row(
                          mainAxisSize: MainAxisSize.min,
                          children: [
                            IconButton(
                              tooltip: 'Decline',
                              onPressed: () => _review(request, approve: false),
                              icon: Icon(Icons.close_rounded, color: Palette.muted),
                            ),
                            IconButton.filled(
                              tooltip: 'Let in',
                              onPressed: () => _review(request, approve: true),
                              style: IconButton.styleFrom(backgroundColor: Palette.primary),
                              icon: const Icon(Icons.check_rounded, color: Palette.onPrimary),
                            ),
                          ],
                        ),
                      ),
                    const SizedBox(height: 16),
                  ],
                  Text(
                    group == null ? 'Members' : '${group.memberCount} ${group.memberCount == 1 ? 'member' : 'members'}',
                    style: text.titleMedium,
                  ),
                  for (final member in _members)
                    person(
                      member,
                      // Admins remove people below them, never themselves.
                      trailing: group != null && group.isAdmin && member.user.id != myId && _rank[member.role]! < myRank
                          ? IconButton(
                              tooltip: 'Remove from group',
                              onPressed: () => _remove(member),
                              icon: Icon(Icons.person_remove_outlined, color: Palette.muted),
                            )
                          : null,
                    ),
                  if (_cursor != null) TextButton(onPressed: _more, child: const Text('Show more')),
                ],
              ),
            ),
    );
  }
}

/// Members' posts waiting for a moderator, oldest first.
class GroupPendingPostsScreen extends ConsumerStatefulWidget {
  const GroupPendingPostsScreen({super.key, required this.groupId});

  final String groupId;

  @override
  ConsumerState<GroupPendingPostsScreen> createState() => _GroupPendingPostsScreenState();
}

class _GroupPendingPostsScreenState extends ConsumerState<GroupPendingPostsScreen> {
  List<Post> _posts = const [];
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final page = await ref.read(groupRepositoryProvider).pendingPosts(widget.groupId);
      if (mounted) {
        setState(() {
          _posts = page.items;
          _loading = false;
          _error = null;
        });
      }
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = error.message;
        });
      }
    }
  }

  Future<void> _review(Post post, {required bool approve}) async {
    try {
      await ref.read(groupRepositoryProvider).reviewPost(widget.groupId, post.id, approve: approve);
      if (!mounted) return;
      setState(() => _posts = [..._posts.where((p) => p.id != post.id)]);
      ScaffoldMessenger.of(context)
          .showSnackBar(SnackBar(content: Text(approve ? 'Approved: it is in the group now.' : 'Rejected.')));
    } on ApiException catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Posts waiting')),
      body: _loading
          ? Center(child: CircularProgressIndicator(color: Palette.primary))
          : RefreshIndicator(
              color: Palette.primary,
              onRefresh: _load,
              child: ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 32),
                children: [
                  if (_error != null) Text(_error!, style: text.bodyMedium),
                  if (_posts.isEmpty && _error == null)
                    Padding(
                      padding: const EdgeInsets.only(top: 48),
                      child: Text('Nothing waiting. All caught up.', textAlign: TextAlign.center, style: text.titleMedium),
                    ),
                  for (final post in _posts)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 14),
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.stretch,
                        children: [
                          PostCard(
                            post: post,
                            onHelpful: () {},
                            onOpen: () => context.push(Routes.post(post.id)),
                            onAuthor: () => openProfile(context, ref, post.author.id),
                          ),
                          const SizedBox(height: 6),
                          Row(
                            children: [
                              Expanded(
                                child: OutlinedButton(
                                  onPressed: () => _review(post, approve: false),
                                  child: const Text('Reject'),
                                ),
                              ),
                              const SizedBox(width: 10),
                              Expanded(
                                child: FilledButton(
                                  onPressed: () => _review(post, approve: true),
                                  child: const Text('Approve'),
                                ),
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
    );
  }
}
