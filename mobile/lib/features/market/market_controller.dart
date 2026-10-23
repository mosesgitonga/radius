import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import 'listing_models.dart';

class MarketState {
  const MarketState({
    this.query = '',
    this.category,
    this.sort = 'newest',
    this.listings = const [],
    this.nextCursor,
    this.radiusKm,
    this.loading = true,
    this.loadingMore = false,
    this.error,
  });

  final String query;
  final String? category;
  final String sort;
  final List<Listing> listings;
  final String? nextCursor;
  final double? radiusKm;
  final bool loading;
  final bool loadingMore;
  final ApiException? error;

  bool get filtered => category != null || query.trim().isNotEmpty;

  MarketState copyWith({
    List<Listing>? listings,
    String? nextCursor,
    bool clearCursor = false,
    double? radiusKm,
    bool? loading,
    bool? loadingMore,
    ApiException? error,
    bool clearError = false,
  }) =>
      MarketState(
        query: query,
        category: category,
        sort: sort,
        listings: listings ?? this.listings,
        nextCursor: clearCursor ? null : (nextCursor ?? this.nextCursor),
        radiusKm: radiusKm ?? this.radiusKm,
        loading: loading ?? this.loading,
        loadingMore: loadingMore ?? this.loadingMore,
        error: clearError ? null : (error ?? this.error),
      );
}

/// Discarded with the signed-in shell, so the next account starts afresh.
final marketControllerProvider = NotifierProvider.autoDispose<MarketController, MarketState>(MarketController.new);

/// Browsing the marketplace: a search, a category and a sort. Changing any of
/// them starts again from the first page.
class MarketController extends Notifier<MarketState> {
  /// An answer to a search already replaced is dropped.
  int _generation = 0;

  @override
  MarketState build() {
    Future.microtask(refresh);
    return const MarketState();
  }

  void setQuery(String query) {
    if (query.trim() == state.query.trim()) return;
    _restart(MarketState(query: query, category: state.category, sort: state.sort));
  }

  void setCategory(String? category) {
    if (category == state.category) return;
    _restart(MarketState(query: state.query, category: category, sort: state.sort));
  }

  void setSort(String sort) {
    if (sort == state.sort) return;
    _restart(MarketState(query: state.query, category: state.category, sort: sort));
  }

  void _restart(MarketState fresh) {
    state = fresh;
    refresh();
  }

  Future<void> refresh() async {
    final generation = ++_generation;
    final current = state;
    state = current.copyWith(loading: true, clearError: true);
    try {
      final page = await ref.read(listingRepositoryProvider).browse(
            query: current.query,
            category: current.category,
            // A search is best ranked by relevance unless a sort was chosen.
            sort: current.query.trim().isNotEmpty && current.sort == 'newest' ? null : current.sort,
          );
      if (generation != _generation) return;
      state = current.copyWith(
        listings: page.items,
        nextCursor: page.nextCursor,
        clearCursor: page.nextCursor == null,
        radiusKm: page.radiusKm,
        loading: false,
        clearError: true,
      );
    } on ApiException catch (error) {
      if (generation != _generation) return;
      state = current.copyWith(listings: const [], loading: false, error: error);
    }
  }

  Future<void> loadMore() async {
    final current = state;
    if (current.nextCursor == null || current.loading || current.loadingMore) return;
    final generation = _generation;
    state = current.copyWith(loadingMore: true);
    try {
      final page = await ref.read(listingRepositoryProvider).browse(
            query: current.query,
            category: current.category,
            sort: current.query.trim().isNotEmpty && current.sort == 'newest' ? null : current.sort,
            cursor: current.nextCursor,
          );
      if (generation != _generation) return;
      final seen = {for (final l in current.listings) l.id};
      state = state.copyWith(
        listings: [...current.listings, ...page.items.where((l) => !seen.contains(l.id))],
        nextCursor: page.nextCursor,
        clearCursor: page.nextCursor == null,
        loadingMore: false,
      );
    } on ApiException catch (error) {
      if (generation != _generation) return;
      state = state.copyWith(loadingMore: false, error: error);
    }
  }

  /// A listing changed elsewhere: show the new one, or drop it once it can no
  /// longer be bought.
  void replace(Listing listing) {
    state = state.copyWith(listings: [
      for (final l in state.listings)
        if (l.id == listing.id) ...[if (listing.isOpen) listing] else l,
    ]);
  }

  void remove(String id) => state = state.copyWith(listings: state.listings.where((l) => l.id != id).toList());
}
