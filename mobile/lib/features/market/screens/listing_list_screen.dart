import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../listing_models.dart';
import '../widgets/listing_card.dart';

/// Which list: what I sell, or what I saved.
enum ListingList {
  mine('mine', 'Your listings', 'Nothing listed yet', 'Sell something you no longer need.'),
  saved('saved', 'Saved', 'Nothing saved yet', 'Tap the bookmark on a listing to keep it here.');

  const ListingList(this.path, this.title, this.emptyTitle, this.emptyBody);
  final String path;
  final String title;
  final String emptyTitle;
  final String emptyBody;
}

class ListingListScreen extends ConsumerStatefulWidget {
  const ListingListScreen({super.key, required this.which});

  final ListingList which;

  @override
  ConsumerState<ListingListScreen> createState() => _ListingListScreenState();
}

class _ListingListScreenState extends ConsumerState<ListingListScreen> {
  final List<Listing> _listings = [];
  String? _cursor;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load({bool more = false}) async {
    try {
      final page =
          await ref.read(listingRepositoryProvider).list(widget.which.path, cursor: more ? _cursor : null);
      if (!mounted) return;
      setState(() {
        if (!more) _listings.clear();
        _listings.addAll(page.items);
        _cursor = page.nextCursor;
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

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: Text(widget.which.title)),
      floatingActionButton: widget.which == ListingList.mine
          ? FloatingActionButton.extended(
              onPressed: () => context.push(Routes.sell),
              backgroundColor: Palette.primary,
              foregroundColor: Palette.white,
              icon: const Icon(Icons.add_rounded),
              label: const Text('Sell'),
            )
          : null,
      body: RefreshIndicator(
        color: Palette.primary,
        onRefresh: _load,
        child: _loading
            ? ListView(children: const [
                SizedBox(height: 120),
                Center(child: CircularProgressIndicator(color: Palette.primary)),
              ])
            : _listings.isEmpty
                ? ListView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.all(32),
                    children: [
                      const SizedBox(height: 32),
                      const Center(child: RadiusMark(size: 48)),
                      const SizedBox(height: 16),
                      Text(_error ?? widget.which.emptyTitle, textAlign: TextAlign.center, style: text.titleLarge),
                      const SizedBox(height: 8),
                      Text(widget.which.emptyBody,
                          textAlign: TextAlign.center, style: text.bodyMedium?.copyWith(color: Palette.muted)),
                    ],
                  )
                : CustomScrollView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    slivers: [
                      SliverPadding(
                        padding: const EdgeInsets.fromLTRB(12, 8, 12, 12),
                        sliver: SliverGrid.builder(
                          itemCount: _listings.length,
                          gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
                            maxCrossAxisExtent: 260,
                            mainAxisSpacing: 10,
                            crossAxisSpacing: 10,
                            childAspectRatio: 0.78,
                          ),
                          itemBuilder: (context, index) {
                            final listing = _listings[index];
                            return ListingCard(
                              listing: listing,
                              onTap: () => context.go(Routes.listing(listing.id)),
                            );
                          },
                        ),
                      ),
                      if (_cursor != null)
                        SliverToBoxAdapter(
                          child: TextButton(onPressed: () => _load(more: true), child: const Text('More')),
                        ),
                      const SliverToBoxAdapter(child: SizedBox(height: 88)),
                    ],
                  ),
      ),
    );
  }
}
