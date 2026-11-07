import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../chats/start_chat.dart';
import '../../profile/profile_screen.dart';
import '../feed_controller.dart';
import '../feed_models.dart';
import '../widgets/post_card.dart';
import '../widgets/report_sheet.dart';

/// One post, its comments, and a box to add one. Top-level comments come highest
/// score first (decision 2.21); each opens its replies in place, oldest first.
class PostScreen extends ConsumerStatefulWidget {
  const PostScreen({super.key, required this.postId});

  final String postId;

  @override
  ConsumerState<PostScreen> createState() => _PostScreenState();
}

class _PostScreenState extends ConsumerState<PostScreen> {
  final _comment = TextEditingController();
  final _commentFocus = FocusNode();
  Post? _post;
  final List<Comment> _comments = [];
  String? _nextCursor;
  bool _loading = true;
  bool _sending = false;
  String? _error;

  /// Replies of each open thread, keyed by its top-level comment, and the cursor
  /// for more. A thread is open while it has an entry here.
  final Map<String, List<Comment>> _replies = {};
  final Map<String, String?> _replyCursors = {};

  /// The comment being answered, if any; the reply joins its thread.
  Comment? _replyingTo;

  @override
  void initState() {
    super.initState();
    // Shown at once from the feed when it is there; refreshed from the server.
    _post = ref.read(feedControllerProvider).posts.where((p) => p.id == widget.postId).firstOrNull;
    _load();
  }

  @override
  void dispose() {
    _comment.dispose();
    _commentFocus.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final repo = ref.read(feedRepositoryProvider);
    try {
      final results = await Future.wait([repo.post(widget.postId), repo.comments(widget.postId)]);
      final page = results[1] as ResultPage<Comment>;
      setState(() {
        _post = results[0] as Post;
        _comments
          ..clear()
          ..addAll(page.items);
        _nextCursor = page.nextCursor;
        _replies.clear();
        _replyCursors.clear();
        _loading = false;
        _error = null;
      });
    } on ApiException catch (error) {
      setState(() {
        _loading = false;
        _error = error.statusCode == 404 ? 'This post is no longer available.' : error.message;
      });
    }
  }

  Future<void> _moreComments() async {
    if (_nextCursor == null) return;
    try {
      final page = await ref.read(feedRepositoryProvider).comments(widget.postId, cursor: _nextCursor);
      setState(() {
        _comments.addAll(page.items);
        _nextCursor = page.nextCursor;
      });
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _send() async {
    final body = _comment.text.trim();
    if (body.isEmpty) return;
    final threadId = _replyingTo?.threadId;
    setState(() => _sending = true);
    try {
      final comment = await ref.read(feedRepositoryProvider).addComment(widget.postId, body, parentId: threadId);
      ref.read(feedControllerProvider.notifier).commentAdded(widget.postId);
      final threadWasOpen = threadId != null && _replies.containsKey(threadId);
      setState(() {
        if (threadId == null) {
          // Unvoted, so it sits after everything scored zero or more.
          final at = _comments.indexWhere((c) => c.score < 0);
          _comments.insert(at < 0 ? _comments.length : at, comment);
        } else {
          _updateComment(threadId, (c) => c.copyWith(replyCount: c.replyCount + 1));
          if (threadWasOpen) _replies[threadId]!.add(comment);
        }
        _comment.clear();
        _replyingTo = null;
        if (_post != null) _post = _post!.copyWith(commentCount: _post!.commentCount + 1);
      });
      // A thread that wasn't open opens now, with the new reply in it.
      if (threadId != null && !threadWasOpen) await _openReplies(threadId);
      if (mounted) FocusScope.of(context).unfocus();
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  void _startReply(Comment comment) {
    setState(() => _replyingTo = comment);
    _commentFocus.requestFocus();
  }

  Future<void> _openReplies(String threadId) async {
    try {
      final page = await ref
          .read(feedRepositoryProvider)
          .comments(widget.postId, parentId: threadId, cursor: _replyCursors[threadId]);
      if (!mounted) return;
      setState(() {
        _replies.update(threadId, (shown) => [...shown, ...page.items], ifAbsent: () => [...page.items]);
        _replyCursors[threadId] = page.nextCursor;
      });
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _closeReplies(String threadId) => setState(() {
    _replies.remove(threadId);
    _replyCursors.remove(threadId);
  });

  /// Replaces a comment wherever it is shown, top level or in a thread.
  void _updateComment(String id, Comment Function(Comment) change) {
    final top = _comments.indexWhere((c) => c.id == id);
    if (top >= 0) _comments[top] = change(_comments[top]);
    for (final thread in _replies.values) {
      final at = thread.indexWhere((c) => c.id == id);
      if (at >= 0) thread[at] = change(thread[at]);
    }
  }

  /// Up or down; the same arrow again takes the vote back. Shown at once, and put
  /// back if the server refuses.
  Future<void> _vote(Comment comment, int value) async {
    final next = comment.myVote == value ? 0 : value;
    setState(
      () => _updateComment(comment.id, (c) => c.copyWith(score: c.score - c.myVote + next, myVote: next)),
    );
    final repo = ref.read(feedRepositoryProvider);
    try {
      final result = next == 0 ? await repo.unvote(comment.id) : await repo.vote(comment.id, next);
      if (mounted) {
        setState(() => _updateComment(comment.id, (c) => c.copyWith(score: result.score, myVote: result.myVote)));
      }
    } on ApiException catch (error) {
      if (mounted) {
        setState(() => _updateComment(comment.id, (c) => c.copyWith(score: comment.score, myVote: comment.myVote)));
      }
      _toast(error.message);
    }
  }

  Future<void> _toggleHelpful() async {
    final controller = ref.read(feedControllerProvider.notifier);
    final inFeed = ref.read(feedControllerProvider).posts.any((p) => p.id == widget.postId);
    try {
      if (inFeed) {
        await controller.toggleHelpful(widget.postId);
        setState(() => _post = ref.read(feedControllerProvider).posts.firstWhere((p) => p.id == widget.postId));
      } else if (_post != null) {
        final repo = ref.read(feedRepositoryProvider);
        if (_post!.myReaction != null) {
          await repo.unreact(widget.postId);
          setState(() => _post = _post!.copyWith(clearReaction: true, reactionCount: _post!.reactionCount - 1));
        } else {
          final count = await repo.react(widget.postId, FeedController.helpful);
          setState(() => _post = _post!.copyWith(myReaction: FeedController.helpful, reactionCount: count));
        }
      }
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _report(String targetType, String targetId, String what) {
    showReportSheet(
      context,
      what: what,
      send: (reason, details) => ref
          .read(feedRepositoryProvider)
          .report(targetType: targetType, targetId: targetId, reason: reason, details: details),
    );
  }

  void _postMenu(Post post) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            ListTile(
              leading: const Icon(Icons.chat_bubble_outline_rounded),
              title: Text('Message ${post.author.displayName}'),
              onTap: () {
                Navigator.of(sheet).pop();
                openDirectChat(context, ref, post.author.id);
              },
            ),
            ListTile(
              leading: const Icon(Icons.flag_outlined),
              title: const Text('Report post'),
              onTap: () {
                Navigator.of(sheet).pop();
                _report('post', post.id, 'post');
              },
            ),
          ],
        ),
      ),
    );
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Widget _tile(Comment comment, String? myId) {
    final mine = comment.author.id == myId;
    return _CommentTile(
      comment: comment,
      onAuthor: () => openProfile(context, ref, comment.author.id),
      onReport: mine ? null : () => _report('comment', comment.id, 'comment'),
      // Nobody votes on their own comment; they still see its score.
      onVote: mine ? null : (value) => _vote(comment, value),
      onReply: () => _startReply(comment),
    );
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final post = _post;
    final replyingTo = _replyingTo;

    return Scaffold(
      appBar: AppBar(title: const Text('Post')),
      body: Column(
        children: [
          Expanded(
            child: post == null
                ? Center(
                    child: _loading
                        ? CircularProgressIndicator(color: Palette.primary)
                        : Padding(
                            padding: const EdgeInsets.all(32),
                            child: Text(_error ?? '', textAlign: TextAlign.center, style: text.bodyLarge),
                          ),
                  )
                : RefreshIndicator(
                    color: Palette.primary,
                    onRefresh: _load,
                    child: ListView(
                      padding: const EdgeInsets.fromLTRB(12, 4, 12, 16),
                      children: [
                        PostCard(
                          post: post,
                          mine: post.author.id == myId,
                          onHelpful: _toggleHelpful,
                          onMore: post.author.id == myId ? null : () => _postMenu(post),
                          onAuthor: () => openProfile(context, ref, post.author.id),
                        ),
                        const SizedBox(height: 16),
                        Text(post.commentCount == 0 ? 'No comments yet' : 'Comments', style: text.titleSmall),
                        const SizedBox(height: 4),
                        for (final comment in _comments) ...[
                          _tile(comment, myId),
                          if (comment.replyCount > 0)
                            _RepliesToggle(
                              count: comment.replyCount,
                              open: _replies.containsKey(comment.id),
                              onTap: () => _replies.containsKey(comment.id)
                                  ? _closeReplies(comment.id)
                                  : _openReplies(comment.id),
                            ),
                          for (final reply in _replies[comment.id] ?? const <Comment>[]) _tile(reply, myId),
                          if (_replyCursors[comment.id] != null)
                            Padding(
                              padding: const EdgeInsets.only(left: _replyIndent),
                              child: Align(
                                alignment: Alignment.centerLeft,
                                child: TextButton(
                                  onPressed: () => _openReplies(comment.id),
                                  child: const Text('Show more replies'),
                                ),
                              ),
                            ),
                        ],
                        if (_nextCursor != null)
                          TextButton(onPressed: _moreComments, child: const Text('Show more comments')),
                        if (_loading) LinearProgressIndicator(color: Palette.primary),
                      ],
                    ),
                  ),
          ),
          if (post != null)
            SafeArea(
              top: false,
              child: Container(
                padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
                decoration: BoxDecoration(
                  color: Palette.surface,
                  border: Border(top: BorderSide(color: Palette.line)),
                ),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (replyingTo != null)
                      Row(
                        children: [
                          Icon(Icons.reply_rounded, size: 16, color: Palette.muted),
                          const SizedBox(width: 6),
                          Expanded(
                            child: Text(
                              'Replying to ${replyingTo.author.displayName}',
                              style: text.bodySmall,
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                          IconButton(
                            onPressed: () => setState(() => _replyingTo = null),
                            tooltip: 'Cancel reply',
                            visualDensity: VisualDensity.compact,
                            icon: Icon(Icons.close_rounded, size: 18, color: Palette.muted),
                          ),
                        ],
                      ),
                    Row(
                      children: [
                        Expanded(
                          child: TextField(
                            controller: _comment,
                            focusNode: _commentFocus,
                            minLines: 1,
                            maxLines: 4,
                            maxLength: 1000,
                            textCapitalization: TextCapitalization.sentences,
                            decoration: InputDecoration(
                              hintText: replyingTo == null ? 'Add a comment' : 'Add a reply',
                              counterText: '',
                              contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
                            ),
                          ),
                        ),
                        const SizedBox(width: 6),
                        IconButton.filled(
                          onPressed: _sending ? null : _send,
                          tooltip: 'Send',
                          style: IconButton.styleFrom(backgroundColor: Palette.primary),
                          icon: _sending
                              ? const SizedBox.square(
                                  dimension: 18,
                                  child: CircularProgressIndicator(strokeWidth: 2, color: Palette.onPrimary),
                                )
                              : const Icon(Icons.arrow_upward_rounded, color: Palette.onPrimary),
                        ),
                      ],
                    ),
                  ],
                ),
              ),
            ),
        ],
      ),
    );
  }
}

/// How far replies sit in from their top-level comment.
const double _replyIndent = 42;

class _CommentTile extends StatelessWidget {
  const _CommentTile({
    required this.comment,
    required this.onAuthor,
    required this.onReply,
    this.onReport,
    this.onVote,
  });

  final Comment comment;
  final VoidCallback onAuthor;
  final VoidCallback onReply;
  final VoidCallback? onReport;

  /// Null on the viewer's own comment.
  final ValueChanged<int>? onVote;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final isReply = comment.parentId != null;
    final meta = [
      if (comment.authorArea != null) comment.authorArea!.label,
      if (comment.isVisiting) 'visiting',
      timeAgo(comment.createdAt),
    ].join(' · ');
    return Padding(
      padding: EdgeInsets.only(top: 8, left: isReply ? _replyIndent : 0),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          GestureDetector(
            onTap: onAuthor,
            child: InitialsAvatar(author: comment.author, radius: isReply ? 13 : 16),
          ),
          const SizedBox(width: 10),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(12),
                  decoration: BoxDecoration(color: Palette.surface, borderRadius: BorderRadius.circular(14)),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Wrap(
                        spacing: 6,
                        crossAxisAlignment: WrapCrossAlignment.center,
                        children: [
                          Text(comment.author.displayName, style: text.titleSmall?.copyWith(fontSize: 14)),
                          if (comment.author.isVerifiedNeighbour) const NeighbourBadge(),
                        ],
                      ),
                      Text(meta, style: text.bodySmall?.copyWith(fontSize: 12)),
                      const SizedBox(height: 4),
                      Text(comment.body, style: text.bodyMedium),
                    ],
                  ),
                ),
                Row(
                  children: [
                    _VoteArrow(up: true, active: comment.myVote == 1, onTap: onVote),
                    Text(
                      '${comment.score}',
                      style: text.labelMedium?.copyWith(
                        color: comment.myVote == 0 ? Palette.inkSoft : _voteColour(up: comment.myVote == 1),
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                    _VoteArrow(up: false, active: comment.myVote == -1, onTap: onVote),
                    const SizedBox(width: 6),
                    TextButton(
                      onPressed: onReply,
                      style: TextButton.styleFrom(
                        foregroundColor: Palette.inkSoft,
                        minimumSize: const Size(0, 36),
                        padding: const EdgeInsets.symmetric(horizontal: 8),
                        textStyle: text.labelMedium,
                      ),
                      child: const Text('Reply'),
                    ),
                  ],
                ),
              ],
            ),
          ),
          if (onReport != null)
            IconButton(
              onPressed: onReport,
              tooltip: 'Report comment',
              icon: Icon(Icons.flag_outlined, size: 18, color: Palette.muted),
            ),
        ],
      ),
    );
  }
}

Color _voteColour({required bool up}) => up ? Palette.primary : Palette.danger;

/// ▲ or ▼. Lit when it is the viewer's vote; tapping it again takes the vote back.
class _VoteArrow extends StatelessWidget {
  const _VoteArrow({required this.up, required this.active, required this.onTap});

  final bool up;
  final bool active;
  final ValueChanged<int>? onTap;

  @override
  Widget build(BuildContext context) {
    final tap = onTap;
    return IconButton(
      onPressed: tap == null ? null : () => tap(up ? 1 : -1),
      tooltip: up ? 'Vote up' : 'Vote down',
      isSelected: active,
      visualDensity: VisualDensity.compact,
      constraints: const BoxConstraints(minWidth: 36, minHeight: 36),
      padding: EdgeInsets.zero,
      icon: Icon(
        up ? Icons.arrow_upward_rounded : Icons.arrow_downward_rounded,
        size: 18,
        color: active ? _voteColour(up: up) : Palette.muted,
      ),
    );
  }
}

/// "3 replies" under a top-level comment, opening or closing them in place.
class _RepliesToggle extends StatelessWidget {
  const _RepliesToggle({required this.count, required this.open, required this.onTap});

  final int count;
  final bool open;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(left: _replyIndent),
      child: Align(
        alignment: Alignment.centerLeft,
        child: TextButton.icon(
          onPressed: onTap,
          style: TextButton.styleFrom(
            foregroundColor: Palette.primary,
            minimumSize: const Size(0, 36),
            padding: const EdgeInsets.symmetric(horizontal: 8),
          ),
          icon: Icon(open ? Icons.expand_less_rounded : Icons.expand_more_rounded, size: 18),
          label: Text(open ? 'Hide replies' : (count == 1 ? '1 reply' : '$count replies')),
        ),
      ),
    );
  }
}
