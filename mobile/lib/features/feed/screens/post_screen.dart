import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../feed_controller.dart';
import '../feed_models.dart';
import '../widgets/post_card.dart';
import '../widgets/report_sheet.dart';

/// One post, its comments, and a box to add one.
class PostScreen extends ConsumerStatefulWidget {
  const PostScreen({super.key, required this.postId});

  final String postId;

  @override
  ConsumerState<PostScreen> createState() => _PostScreenState();
}

class _PostScreenState extends ConsumerState<PostScreen> {
  final _comment = TextEditingController();
  Post? _post;
  final List<Comment> _comments = [];
  String? _nextCursor;
  bool _loading = true;
  bool _sending = false;
  String? _error;

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
    setState(() => _sending = true);
    try {
      final comment = await ref.read(feedRepositoryProvider).addComment(widget.postId, body);
      ref.read(feedControllerProvider.notifier).commentAdded(widget.postId);
      setState(() {
        _comments.add(comment);
        _comment.clear();
        if (_post != null) _post = _post!.copyWith(commentCount: _post!.commentCount + 1);
      });
      if (mounted) FocusScope.of(context).unfocus();
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _sending = false);
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
      send: (reason, details) => ref.read(feedRepositoryProvider).report(
            targetType: targetType,
            targetId: targetId,
            reason: reason,
            details: details,
          ),
    );
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final post = _post;

    return Scaffold(
      appBar: AppBar(title: const Text('Post')),
      body: Column(
        children: [
          Expanded(
            child: post == null
                ? Center(
                    child: _loading
                        ? const CircularProgressIndicator(color: Palette.primary)
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
                          onMore: post.author.id == myId ? null : () => _report('post', post.id, 'post'),
                        ),
                        const SizedBox(height: 16),
                        Text(
                          post.commentCount == 0 ? 'No comments yet' : 'Comments',
                          style: text.titleSmall,
                        ),
                        const SizedBox(height: 8),
                        for (final comment in _comments)
                          _CommentTile(
                            comment: comment,
                            onReport: comment.author.id == myId
                                ? null
                                : () => _report('comment', comment.id, 'comment'),
                          ),
                        if (_nextCursor != null)
                          TextButton(onPressed: _moreComments, child: const Text('Show more comments')),
                        if (_loading) const LinearProgressIndicator(color: Palette.primary),
                      ],
                    ),
                  ),
          ),
          if (post != null)
            SafeArea(
              top: false,
              child: Container(
                padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
                decoration: const BoxDecoration(
                  color: Palette.white,
                  border: Border(top: BorderSide(color: Palette.line)),
                ),
                child: Row(
                  children: [
                    Expanded(
                      child: TextField(
                        controller: _comment,
                        minLines: 1,
                        maxLines: 4,
                        maxLength: 1000,
                        textCapitalization: TextCapitalization.sentences,
                        decoration: const InputDecoration(
                          hintText: 'Add a comment',
                          counterText: '',
                          contentPadding: EdgeInsets.symmetric(horizontal: 14, vertical: 12),
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
                              child: CircularProgressIndicator(strokeWidth: 2, color: Palette.white),
                            )
                          : const Icon(Icons.arrow_upward_rounded, color: Palette.white),
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

class _CommentTile extends StatelessWidget {
  const _CommentTile({required this.comment, this.onReport});

  final Comment comment;
  final VoidCallback? onReport;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final meta = [
      if (comment.authorArea != null) comment.authorArea!.label,
      if (comment.isVisiting) 'visiting',
      timeAgo(comment.createdAt),
    ].join(' · ');
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 8),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          InitialsAvatar(author: comment.author, radius: 16),
          const SizedBox(width: 10),
          Expanded(
            child: Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: Palette.white, borderRadius: BorderRadius.circular(14)),
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
          ),
          if (onReport != null)
            IconButton(
              onPressed: onReport,
              tooltip: 'Report comment',
              icon: const Icon(Icons.flag_outlined, size: 18, color: Palette.muted),
            ),
        ],
      ),
    );
  }
}
