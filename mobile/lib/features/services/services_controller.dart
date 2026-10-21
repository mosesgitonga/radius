import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import 'service_models.dart';

class ServicesState {
  const ServicesState({
    this.kind = ServiceKind.offering,
    this.category,
    this.query = '',
    this.posts = const [],
    this.nextCursor,
    this.radiusKm,
    this.loading = true,
    this.loadingMore = false,
    this.error,
  });

  final ServiceKind kind;
  final String? category;
  final String query;
  final List<ServicePost> posts;
  final String? nextCursor;
  final double? radiusKm;
  final bool loading;
  final bool loadingMore;
  final ApiException? error;

  bool get filtered => category != null || query.trim().isNotEmpty;
}

final servicesControllerProvider =
    NotifierProvider.autoDispose<ServicesController, ServicesState>(ServicesController.new);

/// Browsing services, needs and jobs: one kind at a time, with a category and a
/// search. Changing any of them starts again from the first page.
class ServicesController extends Notifier<ServicesState> {
  /// Each search gets a number; an answer to an older one is dropped, so typing
  /// quickly never shows the results of a search already replaced.
  int _generation = 0;

  @override
  ServicesState build() {
    Future.microtask(refresh);
    return const ServicesState();
  }

  void setKind(ServiceKind kind) {
    if (kind == state.kind) return;
    _restart(ServicesState(kind: kind, category: state.category, query: state.query));
  }

  void setCategory(String? category) {
    if (category == state.category) return;
    _restart(ServicesState(kind: state.kind, category: category, query: state.query));
  }

  void setQuery(String query) {
    if (query.trim() == state.query.trim()) return;
    _restart(ServicesState(kind: state.kind, category: state.category, query: query));
  }

  void _restart(ServicesState fresh) {
    state = fresh;
    refresh();
  }

  Future<void> refresh() async {
    final generation = ++_generation;
    final current = state;
    state = ServicesState(
      kind: current.kind,
      category: current.category,
      query: current.query,
      posts: current.posts,
      radiusKm: current.radiusKm,
    );
    try {
      final page = await ref.read(serviceRepositoryProvider).browse(
            kind: current.kind,
            category: current.category,
            query: current.query,
          );
      if (generation != _generation) return;
      state = ServicesState(
        kind: current.kind,
        category: current.category,
        query: current.query,
        posts: page.items,
        nextCursor: page.nextCursor,
        radiusKm: page.radiusKm,
        loading: false,
      );
    } on ApiException catch (error) {
      if (generation != _generation) return;
      state = ServicesState(
        kind: current.kind,
        category: current.category,
        query: current.query,
        posts: const [],
        loading: false,
        error: error,
      );
    }
  }

  Future<void> loadMore() async {
    final current = state;
    if (current.nextCursor == null || current.loading || current.loadingMore) return;
    final generation = _generation;
    state = _with(current, loadingMore: true);
    try {
      final page = await ref.read(serviceRepositoryProvider).browse(
            kind: current.kind,
            category: current.category,
            query: current.query,
            cursor: current.nextCursor,
          );
      if (generation != _generation) return;
      final seen = {for (final p in current.posts) p.id};
      state = ServicesState(
        kind: current.kind,
        category: current.category,
        query: current.query,
        posts: [...current.posts, ...page.items.where((p) => !seen.contains(p.id))],
        nextCursor: page.nextCursor,
        radiusKm: current.radiusKm,
        loading: false,
      );
    } on ApiException catch (error) {
      if (generation != _generation) return;
      state = _with(state, loadingMore: false, error: error);
    }
  }

  /// A post changed elsewhere (contacted, closed, edited): show the new one.
  void replace(ServicePost post) {
    state = _with(state, posts: [
      for (final p in state.posts)
        if (p.id == post.id) ...[if (post.isOpen && !post.isHeld) post] else p,
    ]);
  }

  ServicesState _with(ServicesState s, {List<ServicePost>? posts, bool? loadingMore, ApiException? error}) =>
      ServicesState(
        kind: s.kind,
        category: s.category,
        query: s.query,
        posts: posts ?? s.posts,
        nextCursor: s.nextCursor,
        radiusKm: s.radiusKm,
        loading: s.loading,
        loadingMore: loadingMore ?? s.loadingMore,
        error: error ?? s.error,
      );
}
