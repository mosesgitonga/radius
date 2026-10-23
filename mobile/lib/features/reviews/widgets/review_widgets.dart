import 'package:flutter/material.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../feed/feed_models.dart';
import '../review_models.dart';

/// Five stars, [rating] of them filled.
class Stars extends StatelessWidget {
  const Stars(this.rating, {super.key, this.size = 15});

  final int rating;
  final double size;

  @override
  Widget build(BuildContext context) {
    return Semantics(
      label: '$rating out of 5 stars',
      child: ExcludeSemantics(
        child: Row(
          mainAxisSize: MainAxisSize.min,
          children: [
            for (var i = 1; i <= 5; i++)
              Icon(i <= rating ? Icons.star_rounded : Icons.star_outline_rounded,
                  size: size, color: Palette.primary),
          ],
        ),
      ),
    );
  }
}

/// What others said about the author: their average, count and reviews.
class ReviewsSection extends StatelessWidget {
  const ReviewsSection({
    super.key,
    required this.name,
    required this.average,
    required this.count,
    required this.reviews,
    required this.hasMore,
    required this.onMore,
    required this.canReply,
    required this.onReply,
    this.onWrite,
  });

  final String name;
  final double? average;
  final int count;
  final List<Review> reviews;
  final bool hasMore;
  final VoidCallback onMore;
  final bool Function(Review) canReply;
  final void Function(Review) onReply;

  /// Shown to someone who has talked with the author about this post.
  final VoidCallback? onWrite;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(color: Palette.white, borderRadius: BorderRadius.circular(16)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Row(
            children: [
              if (average != null && count > 0) ...[
                Text(average!.toStringAsFixed(1), style: text.displaySmall?.copyWith(fontSize: 34)),
                const SizedBox(width: 14),
              ],
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    if (average != null && count > 0) Stars(average!.round(), size: 18),
                    Text(
                      count == 0 ? 'No reviews yet' : (count == 1 ? '1 review' : '$count reviews'),
                      style: text.bodySmall,
                    ),
                  ],
                ),
              ),
              if (onWrite != null) OutlinedButton(onPressed: onWrite, child: const Text('Review')),
            ],
          ),
          for (final review in reviews) ...[
            const Padding(padding: EdgeInsets.symmetric(vertical: 12), child: Divider(height: 1, color: Palette.sand)),
            _ReviewTile(review: review, onReply: canReply(review) ? () => onReply(review) : null),
          ],
          if (hasMore) TextButton(onPressed: onMore, child: const Text('More reviews')),
          const SizedBox(height: 12),
          Text(
            'Only neighbours who dealt with $name through MyRadius can review them.',
            style: text.bodySmall?.copyWith(fontSize: 12),
          ),
        ],
      ),
    );
  }
}

class _ReviewTile extends StatelessWidget {
  const _ReviewTile({required this.review, this.onReply});

  final Review review;
  final VoidCallback? onReply;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(child: Text(review.reviewer.displayName, style: text.titleSmall?.copyWith(fontSize: 14))),
            Stars(review.rating, size: 14),
          ],
        ),
        Text(
          '${review.contextType == 'listing' ? 'Bought or sold' : 'Hired'} through MyRadius · ${timeAgo(review.createdAt)}',
          style: text.bodySmall?.copyWith(fontSize: 12),
        ),
        if (review.body.isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(review.body, style: text.bodyMedium),
        ],
        if (review.replyBody != null)
          Container(
            margin: const EdgeInsets.only(top: 8),
            padding: const EdgeInsets.all(10),
            width: double.infinity,
            decoration: BoxDecoration(color: Palette.cream, borderRadius: BorderRadius.circular(10)),
            child: Text('Reply: ${review.replyBody}', style: text.bodySmall?.copyWith(color: Palette.inkSoft)),
          ),
        if (onReply != null)
          Align(
            alignment: Alignment.centerLeft,
            child: TextButton(
              onPressed: onReply,
              child: Text(review.replyBody == null ? 'Reply' : 'Edit reply'),
            ),
          ),
      ],
    );
  }
}

/// Asks for stars and a few words, then sends the review. Returns it, or null.
Future<Review?> showReviewSheet(
  BuildContext context, {
  required String name,
  required Future<Review> Function(int rating, String? body) send,
}) {
  return showModalBottomSheet<Review>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Palette.cream,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
    builder: (_) => _ReviewSheet(name: name, send: send),
  );
}

class _ReviewSheet extends StatefulWidget {
  const _ReviewSheet({required this.name, required this.send});

  final String name;
  final Future<Review> Function(int rating, String? body) send;

  @override
  State<_ReviewSheet> createState() => _ReviewSheetState();
}

class _ReviewSheetState extends State<_ReviewSheet> {
  int _rating = 0;
  final _body = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _body.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final review = await widget.send(_rating, _body.text);
      if (mounted) Navigator.of(context).pop(review);
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = error.message;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.viewInsetsOf(context).bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Review ${widget.name}', style: text.titleLarge),
          const SizedBox(height: 4),
          Text('Your name and review are public.', style: text.bodySmall),
          const SizedBox(height: 12),
          Row(
            mainAxisAlignment: MainAxisAlignment.center,
            children: [
              for (var i = 1; i <= 5; i++)
                IconButton(
                  tooltip: '$i star${i == 1 ? '' : 's'}',
                  onPressed: () => setState(() => _rating = i),
                  icon: Icon(
                    i <= _rating ? Icons.star_rounded : Icons.star_outline_rounded,
                    size: 36,
                    color: Palette.primary,
                  ),
                ),
            ],
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _body,
            minLines: 3,
            maxLines: 6,
            maxLength: 2000,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(hintText: 'What was it like? (optional)'),
          ),
          if (_error != null) FormError(_error!),
          const SizedBox(height: 8),
          BusyButton(label: 'Post review', busy: _busy, onPressed: _rating == 0 ? null : _submit),
        ],
      ),
    );
  }
}

/// Asks for a reply to a review of me. Returns the updated review, or null.
Future<Review?> showReplySheet(
  BuildContext context, {
  required String to,
  required Future<Review> Function(String body) send,
}) {
  return showModalBottomSheet<Review>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Palette.cream,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
    builder: (_) => _ReplySheet(to: to, send: send),
  );
}

class _ReplySheet extends StatefulWidget {
  const _ReplySheet({required this.to, required this.send});

  final String to;
  final Future<Review> Function(String body) send;

  @override
  State<_ReplySheet> createState() => _ReplySheetState();
}

class _ReplySheetState extends State<_ReplySheet> {
  final _body = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _body.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    if (_body.text.trim().isEmpty) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final review = await widget.send(_body.text);
      if (mounted) Navigator.of(context).pop(review);
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = error.message;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.viewInsetsOf(context).bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Reply to ${widget.to}', style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 12),
          TextField(
            controller: _body,
            minLines: 2,
            maxLines: 5,
            maxLength: 2000,
            autofocus: true,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(hintText: 'Your reply is public, under their review'),
          ),
          if (_error != null) FormError(_error!),
          const SizedBox(height: 8),
          BusyButton(label: 'Post reply', busy: _busy, onPressed: _submit),
        ],
      ),
    );
  }
}
