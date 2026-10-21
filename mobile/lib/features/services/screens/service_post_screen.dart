import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../feed/feed_models.dart';
import '../../feed/widgets/post_card.dart';
import '../../feed/widgets/report_sheet.dart';
import '../contact.dart';
import '../service_models.dart';
import '../services_controller.dart';
import '../widgets/review_widgets.dart';
import '../widgets/service_cards.dart';

/// One service post: who, what, for how much, and what others said.
class ServicePostScreen extends ConsumerStatefulWidget {
  const ServicePostScreen({super.key, required this.postId});

  final String postId;

  @override
  ConsumerState<ServicePostScreen> createState() => _ServicePostScreenState();
}

class _ServicePostScreenState extends ConsumerState<ServicePostScreen> {
  ServicePost? _post;
  ReviewPage? _reviews;
  final List<Review> _reviewList = [];
  bool _loading = true;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final repo = ref.read(serviceRepositoryProvider);
    try {
      final post = await repo.post(widget.postId);
      final reviews = await repo.reviewsOf(post.author.id);
      if (!mounted) return;
      setState(() {
        _post = post;
        _reviews = reviews;
        _reviewList
          ..clear()
          ..addAll(reviews.items);
        _loading = false;
        _error = null;
      });
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() {
        _loading = false;
        _error = error.statusCode == 404 ? 'This post is no longer available.' : error.message;
      });
    }
  }

  Future<void> _moreReviews() async {
    final post = _post;
    final cursor = _reviews?.nextCursor;
    if (post == null || cursor == null) return;
    try {
      final page = await ref.read(serviceRepositoryProvider).reviewsOf(post.author.id, cursor: cursor);
      setState(() {
        _reviews = page;
        _reviewList.addAll(page.items);
      });
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Runs a change to my post and shows the post as the server returns it.
  Future<void> _change(Future<ServicePost> Function() action) async {
    setState(() => _busy = true);
    try {
      final updated = await action();
      if (!mounted) return;
      setState(() => _post = updated);
      _tellList(updated);
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _tellList(ServicePost post) {
    // The browse list may not exist (opened from "Your posts"); nothing to tell then.
    if (ref.exists(servicesControllerProvider)) ref.read(servicesControllerProvider.notifier).replace(post);
  }

  Future<void> _menu(String choice) async {
    final post = _post!;
    switch (choice) {
      case 'edit':
        final updated = await context.push<ServicePost>(Routes.editService(post.id), extra: post);
        if (updated != null && mounted) {
          setState(() => _post = updated);
          _tellList(updated);
        }
      case 'open':
        await _change(() => ref.read(serviceRepositoryProvider).setOpen(post.id, !post.isOpen));
      case 'delete':
        final sure = await showDialog<bool>(
          context: context,
          builder: (dialog) => AlertDialog(
            title: const Text('Delete this post?'),
            content: const Text('It disappears from Services at once. Your reviews stay.'),
            actions: [
              TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Cancel')),
              TextButton(
                onPressed: () => Navigator.of(dialog).pop(true),
                style: TextButton.styleFrom(foregroundColor: Palette.danger),
                child: const Text('Delete'),
              ),
            ],
          ),
        );
        if (sure != true) return;
        try {
          await ref.read(serviceRepositoryProvider).delete(post.id);
          if (ref.exists(servicesControllerProvider)) {
            ref.read(servicesControllerProvider.notifier).replace(_closed(post));
          }
          if (mounted) context.go(Routes.myServices);
        } on ApiException catch (error) {
          _toast(error.message);
        }
      case 'report':
        showReportSheet(
          context,
          what: 'post',
          send: (reason, details) => ref.read(feedRepositoryProvider).report(
                targetType: 'service_post',
                targetId: post.id,
                reason: reason,
                details: details,
              ),
        );
    }
  }

  /// A stand-in that the browse list drops: the post is gone.
  ServicePost _closed(ServicePost post) => ServicePost(
        id: post.id,
        author: post.author,
        kind: post.kind,
        category: post.category,
        title: post.title,
        description: post.description,
        areaLabel: post.areaLabel,
        isOpen: false,
        isMine: true,
        status: post.status,
        createdAt: post.createdAt,
      );

  Future<void> _writeReview() async {
    final post = _post!;
    final review = await showReviewSheet(
      context,
      name: post.author.displayName,
      send: (rating, body) => ref.read(serviceRepositoryProvider).review(
            subjectId: post.author.id,
            postId: post.id,
            rating: rating,
            body: body,
          ),
    );
    if (review != null) await _load();
  }

  Future<void> _reply(Review review) async {
    final updated = await showReplySheet(
      context,
      to: review.reviewer.displayName,
      send: (body) => ref.read(serviceRepositoryProvider).reply(review.id, body),
    );
    if (updated != null) {
      setState(() {
        final index = _reviewList.indexWhere((r) => r.id == review.id);
        if (index >= 0) _reviewList[index] = updated;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final post = _post;

    if (post == null) {
      return Scaffold(
        appBar: AppBar(),
        body: Center(
          child: _loading
              ? const CircularProgressIndicator(color: Palette.primary)
              : Padding(
                  padding: const EdgeInsets.all(32),
                  child: Text(_error ?? '', textAlign: TextAlign.center, style: text.bodyLarge),
                ),
        ),
      );
    }

    final first = post.author.displayName.split(' ').first;
    final contactLabel = post.myConversationId != null
        ? 'Open chat with $first'
        : switch (post.kind) {
            ServiceKind.offering => 'Message $first',
            ServiceKind.seeking => 'I can help',
            ServiceKind.hiring => 'Ask about this job',
          };

    return Scaffold(
      appBar: AppBar(
        actions: [
          PopupMenuButton<String>(
            tooltip: 'More',
            enabled: !_busy,
            onSelected: _menu,
            itemBuilder: (_) => post.isMine
                ? [
                    const PopupMenuItem(value: 'edit', child: Text('Edit')),
                    PopupMenuItem(value: 'open', child: Text(post.isOpen ? 'Close' : 'Reopen')),
                    const PopupMenuItem(value: 'delete', child: Text('Delete')),
                  ]
                : [const PopupMenuItem(value: 'report', child: Text('Report post'))],
          ),
        ],
      ),
      body: RefreshIndicator(
        color: Palette.primary,
        onRefresh: _load,
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 24),
          children: [
            if (post.isMine && post.isHeld)
              _Banner(
                color: Palette.glowTint,
                text: post.heldFor.isEmpty
                    ? 'Waiting for a moderator. Only you can see this post for now.'
                    : 'Waiting for a moderator, because it mentions: ${post.heldFor.map((p) => '"$p"').join(', ')}. '
                        'Real employers never charge job seekers. Only you can see this post for now.',
              ),
            if (!post.isOpen && !post.isHeld)
              _Banner(
                color: Palette.sand,
                text: post.isMine
                    ? 'Closed. Neighbours can no longer find it. Reopen it from the menu.'
                    : 'This post is closed.',
              ),
            const SizedBox(height: 4),
            Center(child: InitialsAvatar(author: post.author, radius: 38)),
            const SizedBox(height: 10),
            Text(post.author.displayName, textAlign: TextAlign.center, style: text.headlineSmall),
            if (post.author.isVerifiedNeighbour) ...[
              const SizedBox(height: 6),
              const Center(child: NeighbourBadge(long: true)),
            ],
            const SizedBox(height: 6),
            Text(
              [
                post.where,
                if (post.travelKm != null) 'travels up to ${post.travelKm} km',
                if (post.kind != ServiceKind.offering) timeAgo(post.createdAt),
              ].join(' · '),
              textAlign: TextAlign.center,
              style: text.bodyMedium?.copyWith(color: Palette.muted),
            ),
            const SizedBox(height: 16),
            if (post.kind == ServiceKind.hiring) ...[
              JobSafetyNote(text: post.safetyNotice),
              const SizedBox(height: 12),
            ],
            _Details(post: post),
            const SizedBox(height: 12),
            ReviewsSection(
              name: first,
              average: _reviews?.average,
              count: _reviews?.count ?? 0,
              reviews: _reviewList,
              hasMore: _reviews?.nextCursor != null,
              onMore: _moreReviews,
              canReply: (review) => post.isMine && review.subjectId == myId,
              onReply: _reply,
              onWrite: !post.isMine && post.myConversationId != null ? _writeReview : null,
            ),
          ],
        ),
      ),
      bottomNavigationBar: post.isMine || !post.isOpen
          ? null
          : SafeArea(
              child: Container(
                padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
                decoration: const BoxDecoration(
                  color: Palette.white,
                  border: Border(top: BorderSide(color: Palette.line)),
                ),
                child: FilledButton.icon(
                  onPressed: () => contactAuthor(context, ref, post),
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52)),
                  icon: const Icon(Icons.chat_bubble_outline_rounded),
                  label: Text(contactLabel),
                ),
              ),
            ),
    );
  }
}

class _Banner extends StatelessWidget {
  const _Banner({required this.color, required this.text});

  final Color color;
  final String text;

  @override
  Widget build(BuildContext context) {
    return Container(
      margin: const EdgeInsets.only(bottom: 12),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: color, borderRadius: BorderRadius.circular(12)),
      child: Text(text, style: Theme.of(context).textTheme.bodySmall?.copyWith(color: Palette.ink, fontSize: 13.5)),
    );
  }
}

class _Details extends StatelessWidget {
  const _Details({required this.post});

  final ServicePost post;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final price = switch (post.kind) {
      ServiceKind.offering => post.rateInfo,
      ServiceKind.seeking => post.budgetInfo,
      ServiceKind.hiring => post.pay,
    };
    final facts = <(String, String)>[
      if (post.kind == ServiceKind.hiring && post.jobTypeName != null) ('Type', post.jobTypeName!),
      if (post.availabilityInfo != null) (post.kind == ServiceKind.hiring ? 'Hours' : 'When', post.availabilityInfo!),
      if (post.kind == ServiceKind.seeking && post.budgetInfo != null) ('Budget', post.budgetInfo!),
    ];
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(color: Palette.white, borderRadius: BorderRadius.circular(16)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(post.categoryName.toUpperCase(),
              style: text.labelSmall?.copyWith(color: Palette.muted, letterSpacing: 0.6)),
          const SizedBox(height: 4),
          Text(post.title, style: text.titleMedium),
          if (price != null && post.kind != ServiceKind.seeking) ...[
            const SizedBox(height: 6),
            Text(price, style: text.titleSmall?.copyWith(color: Palette.primaryDark)),
          ],
          if (post.description.isNotEmpty) ...[
            const SizedBox(height: 10),
            Text(post.description, style: text.bodyMedium?.copyWith(color: Palette.inkSoft, height: 1.45)),
          ],
          for (final (label, value) in facts) ...[
            const SizedBox(height: 10),
            Row(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                SizedBox(width: 72, child: Text(label, style: text.bodySmall)),
                Expanded(child: Text(value, style: text.bodyMedium)),
              ],
            ),
          ],
        ],
      ),
    );
  }
}
