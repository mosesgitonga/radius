import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../feed/widgets/post_card.dart';
import '../../feed/widgets/report_sheet.dart';
import '../../reviews/review_models.dart';
import '../../reviews/widgets/review_widgets.dart';
import '../listing_models.dart';
import '../market_controller.dart';

/// One listing: photos, price, what it is, who sells it, and a way to ask.
class ListingScreen extends ConsumerStatefulWidget {
  const ListingScreen({super.key, required this.listingId});

  final String listingId;

  @override
  ConsumerState<ListingScreen> createState() => _ListingScreenState();
}

class _ListingScreenState extends ConsumerState<ListingScreen> {
  Listing? _listing;
  ReviewPage? _reviews;
  final List<Review> _reviewList = [];
  bool _loading = true;
  bool _busy = false;
  String? _error;
  int _photo = 0;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final listing = await ref.read(listingRepositoryProvider).listing(widget.listingId);
      final reviews = await ref.read(reviewRepositoryProvider).reviewsOf(listing.seller.id);
      if (!mounted) return;
      setState(() {
        _listing = listing;
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
        _error = error.statusCode == 404 ? 'This listing is no longer available.' : error.message;
      });
    }
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Shows a changed listing here and in the browse grid.
  void _show(Listing listing) {
    setState(() => _listing = listing);
    if (ref.exists(marketControllerProvider)) ref.read(marketControllerProvider.notifier).replace(listing);
  }

  Future<void> _change(Future<Listing> Function() action) async {
    setState(() => _busy = true);
    try {
      final updated = await action();
      if (mounted) _show(updated);
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _toggleSaved() async {
    final listing = _listing!;
    final saved = !listing.saved;
    setState(() => _listing = listing.copyWith(saved: saved));
    try {
      // The bookmark itself says so: a message here would cover the Ask button.
      await ref.read(listingRepositoryProvider).save(listing.id, saved);
    } on ApiException catch (error) {
      if (mounted) setState(() => _listing = listing);
      _toast(error.message);
    }
  }

  Future<void> _contact() async {
    final listing = _listing!;
    try {
      final id =
          listing.myConversationId ?? (await ref.read(listingRepositoryProvider).contact(listing.id)).id;
      if (mounted) context.go(Routes.conversation(id));
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _markSold() async {
    final listing = _listing!;
    List<Buyer> buyers;
    try {
      buyers = await ref.read(listingRepositoryProvider).buyers(listing.id);
    } on ApiException catch (error) {
      _toast(error.message);
      return;
    }
    if (!mounted) return;
    // Null is "cancel"; an empty string is "someone not listed".
    final choice = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 20, 20, 8),
              child: Text('Who bought it?', style: Theme.of(sheet).textTheme.titleLarge),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 0, 20, 8),
              child: Text(
                'Optional. If you name them, they are invited to review you.',
                style: Theme.of(sheet).textTheme.bodySmall,
              ),
            ),
            for (final buyer in buyers)
              ListTile(
                leading: InitialsAvatar(author: buyer.user, radius: 18),
                title: Text(buyer.user.displayName),
                onTap: () => Navigator.of(sheet).pop(buyer.user.id),
              ),
            ListTile(
              leading: const Icon(Icons.person_off_outlined),
              title: const Text('Someone else, or I would rather not say'),
              onTap: () => Navigator.of(sheet).pop(''),
            ),
          ],
        ),
      ),
    );
    if (choice == null) return;
    await _change(() => ref
        .read(listingRepositoryProvider)
        .setAvailability(listing.id, 'sold', buyerId: choice.isEmpty ? null : choice));
  }

  Future<void> _menu(String choice) async {
    final listing = _listing!;
    final repo = ref.read(listingRepositoryProvider);
    switch (choice) {
      case 'edit':
        final updated = await context.push<Listing>(Routes.editListing(listing.id), extra: listing);
        if (updated != null && mounted) _show(updated);
      case 'reserve':
        await _change(() => repo.setAvailability(listing.id, 'reserved'));
      case 'available':
        await _change(() => repo.setAvailability(listing.id, 'available'));
      case 'sold':
        await _markSold();
      case 'renew':
        await _change(() => repo.renew(listing.id));
      case 'delete':
        final sure = await showDialog<bool>(
          context: context,
          builder: (dialog) => AlertDialog(
            title: const Text('Delete this listing?'),
            content: const Text('It disappears from the Market at once.'),
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
          await repo.delete(listing.id);
          if (ref.exists(marketControllerProvider)) ref.read(marketControllerProvider.notifier).remove(listing.id);
          if (mounted) context.go(Routes.myListings);
        } on ApiException catch (error) {
          _toast(error.message);
        }
      case 'report':
        showReportSheet(
          context,
          what: 'listing',
          send: (reason, details) => ref.read(feedRepositoryProvider).report(
                targetType: 'listing',
                targetId: listing.id,
                reason: reason,
                details: details,
              ),
        );
    }
  }

  Future<void> _writeReview() async {
    final listing = _listing!;
    final review = await showReviewSheet(
      context,
      name: listing.seller.displayName,
      send: (rating, body) => ref.read(reviewRepositoryProvider).review(
            subjectId: listing.seller.id,
            contextType: 'listing',
            contextId: listing.id,
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
      send: (body) => ref.read(reviewRepositoryProvider).reply(review.id, body),
    );
    if (updated != null) {
      setState(() {
        final index = _reviewList.indexWhere((r) => r.id == review.id);
        if (index >= 0) _reviewList[index] = updated;
      });
    }
  }

  Future<void> _moreReviews() async {
    final listing = _listing;
    final cursor = _reviews?.nextCursor;
    if (listing == null || cursor == null) return;
    try {
      final page = await ref.read(reviewRepositoryProvider).reviewsOf(listing.seller.id, cursor: cursor);
      setState(() {
        _reviews = page;
        _reviewList.addAll(page.items);
      });
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final listing = _listing;

    if (listing == null) {
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

    final banner = listing.isHeld
        ? 'Waiting for a moderator. Only you can see this listing for now.'
        : listing.isSold
            ? (listing.isMine ? 'Sold. It no longer shows in the Market.' : 'This item has been sold.')
            : listing.isExpired
                ? (listing.isMine
                    ? 'Expired. Renew it from the menu to show it again for 30 days.'
                    : 'This listing has expired.')
                : listing.isReserved
                    ? (listing.isMine
                        ? 'Reserved. Buyers see it may come free again.'
                        : 'Reserved for someone, but it may come free again.')
                    : null;

    return Scaffold(
      backgroundColor: Palette.white,
      body: RefreshIndicator(
        color: Palette.primary,
        onRefresh: _load,
        child: CustomScrollView(
          slivers: [
            SliverAppBar(
              pinned: true,
              expandedHeight: 320,
              backgroundColor: Palette.white,
              leading: const _Round(child: BackButton()),
              actions: [
                if (!listing.isMine)
                  _Round(
                    child: IconButton(
                      tooltip: listing.saved ? 'Unsave' : 'Save',
                      onPressed: _toggleSaved,
                      icon: Icon(listing.saved ? Icons.bookmark_rounded : Icons.bookmark_border_rounded),
                    ),
                  ),
                _Round(
                  child: PopupMenuButton<String>(
                    tooltip: 'More',
                    enabled: !_busy,
                    onSelected: _menu,
                    itemBuilder: (_) => listing.isMine
                        ? [
                            if (!listing.isSold) const PopupMenuItem(value: 'edit', child: Text('Edit')),
                            if (listing.isOpen && !listing.isReserved)
                              const PopupMenuItem(value: 'reserve', child: Text('Mark reserved')),
                            if (listing.isReserved && !listing.isExpired)
                              const PopupMenuItem(value: 'available', child: Text('Mark available')),
                            if (!listing.isSold) const PopupMenuItem(value: 'sold', child: Text('Mark sold')),
                            if (listing.isExpired && !listing.isSold)
                              const PopupMenuItem(value: 'renew', child: Text('Renew for 30 days')),
                            const PopupMenuItem(value: 'delete', child: Text('Delete')),
                          ]
                        : [const PopupMenuItem(value: 'report', child: Text('Report listing'))],
                  ),
                ),
                const SizedBox(width: 8),
              ],
              flexibleSpace: FlexibleSpaceBar(
                background: _Photos(listing: listing, onPage: (i) => setState(() => _photo = i), page: _photo),
              ),
            ),
            SliverPadding(
              padding: const EdgeInsets.fromLTRB(18, 16, 18, 24),
              sliver: SliverList.list(
                children: [
                  if (banner != null) ...[
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: listing.isHeld ? Palette.glowTint : Palette.sand,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(banner, style: text.bodySmall?.copyWith(color: Palette.ink, fontSize: 13.5)),
                    ),
                    const SizedBox(height: 14),
                  ],
                  Text(listing.price, style: text.headlineMedium),
                  const SizedBox(height: 4),
                  Text(listing.title, style: text.titleLarge?.copyWith(fontSize: 19)),
                  const SizedBox(height: 10),
                  Wrap(
                    spacing: 8,
                    runSpacing: 8,
                    children: [
                      _Tag('${listing.conditionName} condition', strong: true),
                      if (listing.pricing == 'negotiable') const _Tag('Negotiable'),
                      _Tag(listing.categoryName),
                      _Tag(listing.where),
                    ],
                  ),
                  if (listing.isMine && listing.reachEstimate != null && listing.isOpen) ...[
                    const SizedBox(height: 10),
                    Text('Visible to about ${listing.reachEstimate} neighbours.', style: text.bodySmall),
                  ],
                  if (listing.description.isNotEmpty) ...[
                    const SizedBox(height: 14),
                    Text(listing.description, style: text.bodyLarge?.copyWith(color: Palette.inkSoft, height: 1.5)),
                  ],
                  const SizedBox(height: 16),
                  _SellerCard(listing: listing, average: _reviews?.average, count: _reviews?.count ?? 0),
                  const SizedBox(height: 12),
                  Text(
                    "Meet in a public place and pay on collection. Never send M-Pesa before you've seen the item.",
                    style: text.bodySmall?.copyWith(fontSize: 12.5),
                  ),
                  const SizedBox(height: 16),
                  ReviewsSection(
                    name: listing.seller.displayName.split(' ').first,
                    average: _reviews?.average,
                    count: _reviews?.count ?? 0,
                    reviews: _reviewList,
                    hasMore: _reviews?.nextCursor != null,
                    onMore: _moreReviews,
                    canReply: (review) => listing.isMine && review.subjectId == myId,
                    onReply: _reply,
                    onWrite: !listing.isMine && listing.myConversationId != null ? _writeReview : null,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
      bottomNavigationBar: listing.isMine || (!listing.isOpen && listing.myConversationId == null)
          ? null
          : SafeArea(
              child: Container(
                padding: const EdgeInsets.fromLTRB(18, 12, 18, 12),
                decoration: const BoxDecoration(border: Border(top: BorderSide(color: Palette.line))),
                child: FilledButton.icon(
                  onPressed: _contact,
                  style: FilledButton.styleFrom(minimumSize: const Size.fromHeight(52)),
                  icon: const Icon(Icons.chat_bubble_outline_rounded),
                  label: Text(listing.myConversationId != null ? 'Open chat' : "Ask if it's available"),
                ),
              ),
            ),
    );
  }
}

/// A white disc behind an app-bar button, so it shows on any photo.
class _Round extends StatelessWidget {
  const _Round({required this.child});

  final Widget child;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.all(4),
      child: DecoratedBox(
        decoration: const BoxDecoration(color: Palette.white, shape: BoxShape.circle),
        child: child,
      ),
    );
  }
}

class _Photos extends StatelessWidget {
  const _Photos({required this.listing, required this.page, required this.onPage});

  final Listing listing;
  final int page;
  final ValueChanged<int> onPage;

  @override
  Widget build(BuildContext context) {
    final images = listing.images;
    if (images.isEmpty) {
      return const ColoredBox(
        color: Palette.sand,
        child: Center(child: Icon(Icons.image_outlined, size: 48, color: Palette.muted)),
      );
    }
    final width = (MediaQuery.sizeOf(context).width * MediaQuery.devicePixelRatioOf(context)).round();
    return Stack(
      fit: StackFit.expand,
      children: [
        PageView.builder(
          itemCount: images.length,
          onPageChanged: onPage,
          itemBuilder: (context, index) => CachedNetworkImage(
            imageUrl: images[index].urlFor(width),
            fit: BoxFit.cover,
            placeholder: (_, _) => const ColoredBox(color: Palette.sand),
          ),
        ),
        if (images.length > 1)
          Positioned(
            bottom: 12,
            left: 0,
            right: 0,
            child: Center(
              child: Container(
                padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
                decoration: BoxDecoration(
                  color: Palette.ink.withValues(alpha: 0.75),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Text('${page + 1} / ${images.length}',
                    style: const TextStyle(color: Palette.white, fontSize: 12)),
              ),
            ),
          ),
      ],
    );
  }
}

class _Tag extends StatelessWidget {
  const _Tag(this.label, {this.strong = false});

  final String label;
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: strong ? Palette.glowTint : Palette.cream,
        borderRadius: BorderRadius.circular(13),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: 12.5,
          color: strong ? Palette.primaryDark : Palette.inkSoft,
          fontWeight: strong ? FontWeight.w600 : FontWeight.w400,
        ),
      ),
    );
  }
}

class _SellerCard extends StatelessWidget {
  const _SellerCard({required this.listing, required this.average, required this.count});

  final Listing listing;
  final double? average;
  final int count;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final rating = average == null || count == 0
        ? 'No reviews yet'
        : '★ ${average!.toStringAsFixed(1)} from $count review${count == 1 ? '' : 's'}';
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Palette.line),
      ),
      child: Row(
        children: [
          InitialsAvatar(author: listing.seller, radius: 22),
          const SizedBox(width: 12),
          Expanded(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Wrap(
                  spacing: 6,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    Text(listing.seller.displayName, style: text.titleSmall),
                    if (listing.seller.isVerifiedNeighbour) const NeighbourBadge(),
                  ],
                ),
                Text(rating, style: text.bodySmall?.copyWith(fontSize: 13)),
              ],
            ),
          ),
        ],
      ),
    );
  }
}
