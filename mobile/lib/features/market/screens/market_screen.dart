import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../listing_models.dart';
import '../market_controller.dart';
import '../widgets/listing_card.dart';

/// The Market tab: what neighbours are selling, nearest first if asked.
class MarketScreen extends ConsumerStatefulWidget {
  const MarketScreen({super.key});

  @override
  ConsumerState<MarketScreen> createState() => _MarketScreenState();
}

class _MarketScreenState extends ConsumerState<MarketScreen> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  Timer? _debounce;

  MarketController get _controller => ref.read(marketControllerProvider.notifier);

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) _controller.loadMore();
    });
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  /// Searches once typing pauses, not on every key.
  void _onSearch(String value) {
    setState(() {});
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), () => _controller.setQuery(value));
  }

  Future<void> _chooseSort(String current) async {
    final chosen = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: RadioGroup<String>(
          groupValue: current,
          onChanged: (value) => Navigator.of(sheet).pop(value),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              for (final entry in listingSorts.entries)
                RadioListTile<String>(value: entry.key, title: Text(entry.value)),
            ],
          ),
        ),
      ),
    );
    if (chosen != null) _controller.setSort(chosen);
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final state = ref.watch(marketControllerProvider);

    return Scaffold(
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 14, 8, 8),
              child: Row(
                children: [
                  Expanded(child: Text('Marketplace', style: text.headlineSmall)),
                  IconButton(
                    tooltip: 'Saved',
                    onPressed: () => context.go(Routes.savedListings),
                    icon: const Icon(Icons.bookmark_border_rounded),
                  ),
                  IconButton(
                    tooltip: 'Your listings',
                    onPressed: () => context.go(Routes.myListings),
                    icon: const Icon(Icons.storefront_outlined),
                  ),
                  FilledButton.icon(
                    onPressed: () => context.push(Routes.sell),
                    style: FilledButton.styleFrom(
                      minimumSize: const Size(0, 40),
                      padding: const EdgeInsets.symmetric(horizontal: 14),
                    ),
                    icon: const Icon(Icons.add_rounded, size: 18),
                    label: const Text('Sell'),
                  ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: TextField(
                controller: _search,
                onChanged: _onSearch,
                textInputAction: TextInputAction.search,
                decoration: InputDecoration(
                  hintText: 'Search sofas, phones, mitumba…',
                  prefixIcon: Icon(Icons.search_rounded, color: Palette.muted),
                  suffixIcon: _search.text.isEmpty
                      ? null
                      : IconButton(
                          tooltip: 'Clear',
                          icon: const Icon(Icons.close_rounded),
                          onPressed: () {
                            _search.clear();
                            _onSearch('');
                          },
                        ),
                ),
              ),
            ),
            SizedBox(
              height: 52,
              child: ListView(
                scrollDirection: Axis.horizontal,
                padding: const EdgeInsets.fromLTRB(16, 10, 16, 6),
                children: [
                  _Chip(
                    label: listingSorts[state.sort] ?? 'Newest',
                    icon: Icons.swap_vert_rounded,
                    selected: true,
                    onTap: () => _chooseSort(state.sort),
                  ),
                  _Chip(label: 'All', selected: state.category == null, onTap: () => _controller.setCategory(null)),
                  for (final entry in listingCategories.entries)
                    _Chip(
                      label: entry.value,
                      selected: state.category == entry.key,
                      onTap: () => _controller.setCategory(entry.key),
                    ),
                ],
              ),
            ),
            Expanded(
              child: RefreshIndicator(
                color: Palette.primary,
                onRefresh: _controller.refresh,
                child: _grid(state, text),
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _grid(MarketState state, TextTheme text) {
    if (state.loading && state.listings.isEmpty) {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [SizedBox(height: 120), Center(child: CircularProgressIndicator(color: Palette.primary))],
      );
    }
    if (state.listings.isEmpty) {
      final (title, body) = state.error != null
          ? ("Couldn't load the marketplace", state.error!.message)
          : state.filtered
              ? ('Nothing matches', 'Try another word, or all categories.')
              : ('Nothing for sale nearby yet', 'Sell something you no longer need. Neighbours nearby will see it.');
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.all(32),
        children: [
          const SizedBox(height: 24),
          const Center(child: RadiusMark(size: 48)),
          const SizedBox(height: 16),
          Text(title, textAlign: TextAlign.center, style: text.titleLarge),
          const SizedBox(height: 8),
          Text(body, textAlign: TextAlign.center, style: text.bodyMedium?.copyWith(color: Palette.muted)),
        ],
      );
    }
    return CustomScrollView(
      controller: _scroll,
      physics: const AlwaysScrollableScrollPhysics(),
      slivers: [
        if (state.radiusKm != null)
          SliverPadding(
            padding: const EdgeInsets.fromLTRB(16, 2, 16, 8),
            sliver: SliverToBoxAdapter(
              child: Text('Within ${_km(state.radiusKm!)} km of home', style: text.bodySmall),
            ),
          ),
        SliverPadding(
          padding: const EdgeInsets.fromLTRB(12, 0, 12, 24),
          sliver: SliverGrid.builder(
            itemCount: state.listings.length,
            gridDelegate: const SliverGridDelegateWithMaxCrossAxisExtent(
              maxCrossAxisExtent: 260,
              mainAxisSpacing: 10,
              crossAxisSpacing: 10,
              // Photo (4:3) and three lines of text below it.
              childAspectRatio: 0.78,
            ),
            itemBuilder: (context, index) {
              final listing = state.listings[index];
              return ListingCard(listing: listing, onTap: () => context.go(Routes.listing(listing.id)));
            },
          ),
        ),
        if (state.loadingMore)
          SliverToBoxAdapter(
            child: Padding(
              padding: EdgeInsets.all(16),
              child: Center(child: CircularProgressIndicator(color: Palette.primary)),
            ),
          ),
      ],
    );
  }

  static String _km(double value) =>
      value == value.roundToDouble() ? value.toStringAsFixed(0) : value.toStringAsFixed(1);
}

class _Chip extends StatelessWidget {
  const _Chip({required this.label, required this.selected, required this.onTap, this.icon});

  final String label;
  final bool selected;
  final VoidCallback onTap;
  final IconData? icon;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        avatar: icon == null ? null : Icon(icon, size: 16, color: selected ? Palette.onPrimary : Palette.ink),
        label: Text(label),
        selected: selected,
        onSelected: (_) => onTap(),
        showCheckmark: false,
        selectedColor: Palette.ink,
        labelStyle: TextStyle(color: selected ? Palette.onPrimary : Palette.ink, fontSize: 13.5),
        backgroundColor: Palette.surface,
        side: BorderSide(color: selected ? Palette.ink : Palette.lineStrong),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(17)),
      ),
    );
  }
}
