import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import 'feed_models.dart';

class FeedState {
  const FeedState({
    this.posts = const [],
    this.nextCursor,
    this.radiusKm,
    this.loading = true,
    this.loadingMore = false,
    this.error,
  });

  final List<Post> posts;
  final String? nextCursor;
  final double? radiusKm;
  final bool loading;
  final bool loadingMore;
  final ApiException? error;

  bool get hasMore => nextCursor != null;

  FeedState copyWith({
    List<Post>? posts,
    String? nextCursor,
    bool clearCursor = false,
    double? radiusKm,
    bool? loading,
    bool? loadingMore,
    ApiException? error,
    bool clearError = false,
  }) {
    return FeedState(
      posts: posts ?? this.posts,
      nextCursor: clearCursor ? null : (nextCursor ?? this.nextCursor),
      radiusKm: radiusKm ?? this.radiusKm,
      loading: loading ?? this.loading,
      loadingMore: loadingMore ?? this.loadingMore,
      error: clearError ? null : (error ?? this.error),
    );
  }
}

final feedControllerProvider = NotifierProvider<FeedController, FeedState>(FeedController.new);

/// The neighbourhood feed: first page, more on scroll, pull to refresh, and
/// local updates when the person reacts, comments or posts.
class FeedController extends Notifier<FeedState> {
  static const helpful = 'helpful';

  @override
  FeedState build() {
    Future.microtask(refresh);
    return const FeedState();
  }

  Future<void> refresh() async {
    state = state.copyWith(loading: true, clearError: true);
    try {
      final page = await ref.read(feedRepositoryProvider).feed();
      state = FeedState(
        posts: page.items,
        nextCursor: page.nextCursor,
        radiusKm: page.radiusKm,
        loading: false,
      );
    } on ApiException catch (error) {
      state = state.copyWith(loading: false, error: error);
    }
  }

  Future<void> loadMore() async {
    if (!state.hasMore || state.loadingMore || state.loading) return;
    state = state.copyWith(loadingMore: true);
    try {
      final page = await ref.read(feedRepositoryProvider).feed(cursor: state.nextCursor);
      final seen = {for (final post in state.posts) post.id};
      state = state.copyWith(
        posts: [...state.posts, ...page.items.where((post) => !seen.contains(post.id))],
        nextCursor: page.nextCursor,
        clearCursor: page.nextCursor == null,
        loadingMore: false,
      );
    } on ApiException catch (error) {
      state = state.copyWith(loadingMore: false, error: error);
    }
  }

  /// Marks a post helpful, or unmarks it. Shown at once and put back if the
  /// server refuses.
  Future<void> toggleHelpful(String postId) async {
    final post = state.posts.where((p) => p.id == postId).firstOrNull;
    if (post == null) return;
    final on = post.myReaction != null;
    _replace(on
        ? post.copyWith(clearReaction: true, reactionCount: post.reactionCount - 1)
        : post.copyWith(myReaction: helpful, reactionCount: post.reactionCount + 1));
    try {
      final repo = ref.read(feedRepositoryProvider);
      if (on) {
        await repo.unreact(postId);
      } else {
        final count = await repo.react(postId, helpful);
        final current = state.posts.where((p) => p.id == postId).firstOrNull;
        if (current != null) _replace(current.copyWith(reactionCount: count));
      }
    } on ApiException {
      _replace(post);
      rethrow;
    }
  }

  void replace(Post post) => _replace(post);

  void prepend(Post post) =>
      state = state.copyWith(posts: [post, ...state.posts.where((p) => p.id != post.id)]);

  void commentAdded(String postId) {
    final post = state.posts.where((p) => p.id == postId).firstOrNull;
    if (post != null) _replace(post.copyWith(commentCount: post.commentCount + 1));
  }

  void _replace(Post post) {
    state = state.copyWith(posts: [for (final p in state.posts) p.id == post.id ? post : p]);
  }
}
