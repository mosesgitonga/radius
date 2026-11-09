import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../chats/start_chat.dart';
import '../../feed/feed_controller.dart';
import '../../feed/feed_models.dart';
import '../../feed/widgets/post_card.dart';
import '../../feed/widgets/report_sheet.dart';
import '../../profile/profile_screen.dart';
import '../group_models.dart';
import '../widgets/group_widgets.dart';

/// One group: who and where it is, the button that fits where you stand, and its
/// posts, pinned ones first. Admins reach their tools from the menu.
class GroupScreen extends ConsumerStatefulWidget {
  const GroupScreen({super.key, required this.groupId});

  final String groupId;

  @override
  ConsumerState<GroupScreen> createState() => _GroupScreenState();
}

class _GroupScreenState extends ConsumerState<GroupScreen> {
  final _scroll = ScrollController();
  Group? _group;
  List<Post> _pinned = const [];
  List<Post> _posts = const [];
  String? _cursor;
  bool _loading = true;
  bool _loadingMore = false;
  bool _busy = false;

  /// Why the posts can't be shown, e.g. a private group you're invited to.
  bool _postsHidden = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) _more();
    });
    _load();
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final repo = ref.read(groupRepositoryProvider);
    try {
      final group = await repo.group(widget.groupId);
      GroupPostPage? page;
      var hidden = false;
      if (group.isPublic || group.isMember) {
        try {
          page = await repo.posts(widget.groupId);
        } on ApiException catch (error) {
          if (error.statusCode != 404 && error.statusCode != 403) rethrow;
          hidden = true;
        }
      } else {
        hidden = true;
      }
      if (!mounted) return;
      setState(() {
        _group = group;
        _pinned = page?.pinned ?? const [];
        _posts = page?.items ?? const [];
        _cursor = page?.nextCursor;
        _postsHidden = hidden;
        _loading = false;
        _error = null;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = error.statusCode == 404 ? 'This group is no longer available.' : error.message;
      });
    }
  }

  Future<void> _more() async {
    if (_cursor == null || _loadingMore) return;
    setState(() => _loadingMore = true);
    try {
      final page = await ref.read(groupRepositoryProvider).posts(widget.groupId, cursor: _cursor);
      if (!mounted) return;
      final seen = {for (final post in _posts) post.id};
      setState(() {
        _posts = [..._posts, ...page.items.where((post) => !seen.contains(post.id))];
        _cursor = page.nextCursor;
      });
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  /// Runs a membership change and shows the group as the server now has it.
  Future<void> _act(Future<Group?> Function() action, {String? done}) async {
    setState(() => _busy = true);
    try {
      final group = await action();
      if (!mounted) return;
      if (group != null) setState(() => _group = group);
      if (done != null) _toast(done);
      await _load();
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _join() => _act(() => ref.read(groupRepositoryProvider).join(widget.groupId));

  void _answer({required bool accept}) =>
      _act(() => ref.read(groupRepositoryProvider).answerInvitation(widget.groupId, accept: accept));

  Future<void> _leave({required bool request}) async {
    final group = _group!;
    final sure = request ||
        await showDialog<bool>(
              context: context,
              builder: (dialog) => AlertDialog(
                title: Text('Leave ${group.name}?'),
                content: Text(group.isPublic
                    ? 'You can join again later.'
                    : "It's private, so you'd need a new invitation to come back."),
                actions: [
                  TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Stay')),
                  TextButton(onPressed: () => Navigator.of(dialog).pop(true), child: const Text('Leave')),
                ],
              ),
            ) ==
            true;
    if (!sure) return;
    await _act(() async {
      await ref.read(groupRepositoryProvider).leave(widget.groupId);
      return null;
    }, done: request ? 'Request taken back.' : 'You left ${group.name}.');
  }

  Future<void> _copyInviteLink() async {
    try {
      final token = await ref.read(groupRepositoryProvider).createInviteLink(widget.groupId);
      await Clipboard.setData(ClipboardData(text: inviteLinkFor(token)));
      _toast('Invite link copied. It works for 7 days; paste it into a chat.');
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _enableChat() => _act(() => ref.read(groupRepositoryProvider).enableChat(widget.groupId),
      done: 'Group chat is on. Everyone in the group is in it.');

  Future<void> _write() async {
    final group = _group!;
    final post = await context.push<Post>(Routes.groupCompose(group.id), extra: group.name);
    if (post != null && mounted) setState(() => _posts = [post, ..._posts]);
  }

  Future<void> _edit() async {
    final group = await context.push<Group>(Routes.editGroup(widget.groupId), extra: _group);
    if (group != null && mounted) setState(() => _group = group);
  }

  void _replacePost(String id, Post Function(Post) change) => setState(() {
        _posts = [for (final post in _posts) post.id == id ? change(post) : post];
        _pinned = [for (final post in _pinned) post.id == id ? change(post) : post];
      });

  Future<void> _toggleHelpful(Post post) async {
    final repo = ref.read(feedRepositoryProvider);
    try {
      if (post.myReaction != null) {
        _replacePost(post.id, (p) => p.copyWith(clearReaction: true, reactionCount: p.reactionCount - 1));
        await repo.unreact(post.id);
      } else {
        _replacePost(post.id, (p) => p.copyWith(myReaction: FeedController.helpful, reactionCount: p.reactionCount + 1));
        final count = await repo.react(post.id, FeedController.helpful);
        _replacePost(post.id, (p) => p.copyWith(reactionCount: count));
      }
    } on ApiException catch (error) {
      _replacePost(post.id, (_) => post);
      _toast(error.message);
    }
  }

  Future<void> _moderate(Future<void> Function() action, String done) async {
    try {
      await action();
      _toast(done);
      await _load();
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _postMenu(Post post, {required bool mine}) {
    final group = _group!;
    final repo = ref.read(groupRepositoryProvider);
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) {
        void run(VoidCallback action) {
          Navigator.of(sheet).pop();
          action();
        }

        return SafeArea(
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (group.isAdmin && !post.awaitsApproval)
                ListTile(
                  leading: Icon(post.pinned ? Icons.push_pin_outlined : Icons.push_pin_rounded),
                  title: Text(post.pinned ? 'Unpin' : 'Pin to the top'),
                  onTap: () => run(() => _moderate(
                        () => repo.pin(group.id, post.id, pinned: !post.pinned),
                        post.pinned ? 'Unpinned.' : 'Pinned.',
                      )),
                ),
              if (group.isModerator && !mine)
                ListTile(
                  leading: const Icon(Icons.remove_circle_outline_rounded),
                  title: const Text('Remove from the group'),
                  onTap: () => run(() => _moderate(() => repo.removePost(group.id, post.id), 'Post removed.')),
                ),
              if (!mine)
                ListTile(
                  leading: const Icon(Icons.chat_bubble_outline_rounded),
                  title: Text('Message ${post.author.displayName}'),
                  onTap: () => run(() => openDirectChat(context, ref, post.author.id)),
                ),
              if (!mine)
                ListTile(
                  leading: const Icon(Icons.flag_outlined),
                  title: const Text('Report post'),
                  onTap: () => run(() => showReportSheet(
                        context,
                        what: 'post',
                        send: (reason, details) => ref
                            .read(feedRepositoryProvider)
                            .report(targetType: 'post', targetId: post.id, reason: reason, details: details),
                      )),
                ),
              ListTile(
                leading: const Icon(Icons.close_rounded),
                title: const Text('Cancel'),
                onTap: () => Navigator.of(sheet).pop(),
              ),
            ],
          ),
        );
      },
    );
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final group = _group;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;

    return Scaffold(
      appBar: AppBar(
        title: Text(group?.name ?? 'Group', overflow: TextOverflow.ellipsis),
        actions: [if (group != null) _menu(group)],
      ),
      floatingActionButton: group != null && group.isMember
          ? FloatingActionButton(
              onPressed: _write,
              tooltip: 'Post in the group',
              backgroundColor: Palette.primary,
              foregroundColor: Palette.onPrimary,
              child: const Icon(Icons.edit_rounded),
            )
          : null,
      body: group == null
          ? Center(
              child: _loading
                  ? CircularProgressIndicator(color: Palette.primary)
                  : Padding(
                      padding: const EdgeInsets.all(32),
                      child: Text(_error ?? '', textAlign: TextAlign.center),
                    ),
            )
          : RefreshIndicator(
              color: Palette.primary,
              onRefresh: _load,
              child: ListView(
                controller: _scroll,
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 0, 12, 96),
                children: [
                  _Header(group: group),
                  const SizedBox(height: 12),
                  _actions(group),
                  const SizedBox(height: 16),
                  ..._postList(group, myId),
                ],
              ),
            ),
    );
  }

  Widget _menu(Group group) {
    final items = <(String, IconData, VoidCallback)>[
      ('Members', Icons.people_outline_rounded, () => context.go(Routes.groupMembers(group.id))),
      if (group.isAdmin) ('Edit group', Icons.edit_outlined, _edit),
      if (group.isModerator && group.approvesPosts)
        ('Posts waiting', Icons.pending_actions_rounded, () => context.go(Routes.groupPending(group.id))),
      if (group.isAdmin) ('Copy invite link', Icons.link_rounded, _copyInviteLink),
      if (group.isAdmin && group.chatId == null) ('Turn on group chat', Icons.forum_outlined, _enableChat),
      if (group.isMember) ('Leave group', Icons.logout_rounded, () => _leave(request: false)),
    ];
    return PopupMenuButton<int>(
      tooltip: 'More',
      onSelected: (index) => items[index].$3(),
      itemBuilder: (_) => [
        for (var i = 0; i < items.length; i++)
          PopupMenuItem(
            value: i,
            child: Row(children: [Icon(items[i].$2, size: 20), const SizedBox(width: 12), Text(items[i].$1)]),
          ),
      ],
    );
  }

  Widget _actions(Group group) {
    if (group.isMember) {
      return Row(
        children: [
          if (group.chatId != null)
            Expanded(
              child: OutlinedButton.icon(
                onPressed: () => context.go(Routes.conversation(group.chatId!)),
                icon: const Icon(Icons.forum_outlined),
                label: const Text('Group chat'),
              ),
            ),
          if (group.chatId != null) const SizedBox(width: 10),
          Expanded(
            child: FilledButton.icon(
              onPressed: _write,
              icon: const Icon(Icons.edit_rounded, size: 18),
              label: const Text('Write a post'),
            ),
          ),
        ],
      );
    }
    if (group.isInvited) {
      return Row(
        children: [
          Expanded(
            child: OutlinedButton(
              onPressed: _busy ? null : () => _answer(accept: false),
              child: const Text('Decline'),
            ),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: BusyButton(label: 'Accept invitation', busy: _busy, onPressed: () => _answer(accept: true)),
          ),
        ],
      );
    }
    if (group.hasAsked) {
      return Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const NotePanel(
            icon: Icons.hourglass_top_rounded,
            text: "You've asked to join. An admin will let you in.",
          ),
          const SizedBox(height: 8),
          OutlinedButton(
            onPressed: _busy ? null : () => _leave(request: true),
            child: const Text('Take back request'),
          ),
        ],
      );
    }
    if (!group.isPublic) {
      return const NotePanel(
        icon: Icons.lock_outline_rounded,
        text: 'This group is private. You can join it through an invitation or an invite link.',
      );
    }
    return BusyButton(
      label: group.needsApproval ? 'Ask to join' : 'Join group',
      busy: _busy,
      onPressed: _join,
    );
  }

  List<Widget> _postList(Group group, String? myId) {
    final text = Theme.of(context).textTheme;
    if (_postsHidden) {
      return [Text("Posts are shown to the group's members.", style: text.bodyMedium?.copyWith(color: Palette.muted))];
    }
    Widget card(Post post) {
      final mine = post.author.id == myId;
      return Padding(
        padding: const EdgeInsets.only(bottom: 10),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            if (post.pinned || post.awaitsApproval)
              Padding(
                padding: const EdgeInsets.only(left: 4, bottom: 4),
                child: Row(
                  children: [
                    Icon(
                      post.pinned ? Icons.push_pin_rounded : Icons.hourglass_top_rounded,
                      size: 14,
                      color: post.pinned ? Palette.primary : Palette.muted,
                    ),
                    const SizedBox(width: 4),
                    Text(
                      post.pinned ? 'Pinned' : 'Waiting for approval: only you and moderators see it',
                      style: text.labelSmall?.copyWith(color: post.pinned ? Palette.primary : Palette.muted),
                    ),
                  ],
                ),
              ),
            PostCard(
              post: post,
              mine: mine,
              onHelpful: () => _toggleHelpful(post),
              onOpen: () => context.push(Routes.post(post.id)),
              onMore: () => _postMenu(post, mine: mine),
              onAuthor: () => openProfile(context, ref, post.author.id),
            ),
          ],
        ),
      );
    }

    final pinnedIds = {for (final post in _pinned) post.id};
    final rest = [for (final post in _posts) if (!pinnedIds.contains(post.id)) post];
    if (_pinned.isEmpty && rest.isEmpty) {
      return [
        const SizedBox(height: 24),
        const Center(child: RadiusMark(size: 44)),
        const SizedBox(height: 12),
        Text('No posts yet', textAlign: TextAlign.center, style: text.titleMedium),
        const SizedBox(height: 4),
        Text(
          group.isMember ? 'Start the conversation.' : 'Join to start the conversation.',
          textAlign: TextAlign.center,
          style: text.bodyMedium?.copyWith(color: Palette.muted),
        ),
      ];
    }
    return [
      for (final post in _pinned) card(post),
      for (final post in rest) card(post),
      if (_loadingMore) Center(child: CircularProgressIndicator(color: Palette.primary)),
    ];
  }
}

class _Header extends StatelessWidget {
  const _Header({required this.group});

  final Group group;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final cover = sizeFor(group.cover, (MediaQuery.sizeOf(context).width * MediaQuery.devicePixelRatioOf(context)).round());
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        if (cover != null) ...[
          ClipRRect(
            borderRadius: BorderRadius.circular(16),
            child: AspectRatio(
              aspectRatio: 16 / 7,
              child: CachedNetworkImage(imageUrl: cover, fit: BoxFit.cover),
            ),
          ),
          const SizedBox(height: 12),
        ] else
          const SizedBox(height: 4),
        Row(
          children: [
            if (cover == null) ...[GroupBadge(group: group, size: 56), const SizedBox(width: 12)],
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(group.name, style: text.headlineSmall),
                  const SizedBox(height: 2),
                  Text(
                    [
                      group.isPublic ? 'Public' : 'Private',
                      group.categoryLabel,
                      group.meta,
                    ].join(' · '),
                    style: text.bodySmall,
                  ),
                ],
              ),
            ),
          ],
        ),
        if (group.description.isNotEmpty) ...[
          const SizedBox(height: 10),
          Text(group.description, style: text.bodyMedium),
        ],
        if (group.rules != null && group.rules!.trim().isNotEmpty)
          Theme(
            data: Theme.of(context).copyWith(dividerColor: Colors.transparent),
            child: ExpansionTile(
              tilePadding: EdgeInsets.zero,
              title: Text('Group rules', style: text.titleSmall),
              leading: Icon(Icons.rule_rounded, color: Palette.muted),
              children: [
                Align(alignment: Alignment.centerLeft, child: Text(group.rules!, style: text.bodyMedium)),
                const SizedBox(height: 8),
              ],
            ),
          ),
        if (group.approvesPosts && group.isMember)
          Padding(
            padding: const EdgeInsets.only(top: 8),
            child: Text(
              group.isModerator ? 'Members’ posts wait for approval.' : 'Posts here are approved by a moderator first.',
              style: text.bodySmall,
            ),
          ),
      ],
    );
  }
}
