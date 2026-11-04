import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../auth/auth_controller.dart';
import 'story_models.dart';

class StoriesState {
  const StoriesState({this.stories = const [], this.loading = true, this.error});

  final List<Story> stories;
  final bool loading;
  final ApiException? error;
}

/// Stories nearby, for the strip on Home and the viewer. Discarded with the
/// signed-in shell, like the feed.
final storiesControllerProvider = NotifierProvider.autoDispose<StoriesController, StoriesState>(StoriesController.new);

class StoriesController extends Notifier<StoriesState> {
  @override
  StoriesState build() {
    Future.microtask(refresh);
    return const StoriesState();
  }

  String get _myId {
    final auth = ref.read(authControllerProvider);
    return auth is AuthSignedIn ? auth.me.id : '';
  }

  /// Grouped by person, mine first.
  List<StoryGroup> get groups => groupStories(state.stories, myId: _myId);

  Future<void> refresh() async {
    try {
      final stories = await ref.read(storyRepositoryProvider).nearby();
      state = StoriesState(stories: stories, loading: false);
    } on ApiException catch (error) {
      state = StoriesState(stories: state.stories, loading: false, error: error);
    }
  }

  /// Watched: ringed grey at once, and told to the server. A failure to tell it
  /// is not worth interrupting the story for.
  Future<void> markSeen(Story story) async {
    if (story.seen) return;
    state = StoriesState(
      stories: [for (final s in state.stories) s.id == story.id ? s.asSeen() : s],
      loading: state.loading,
      error: state.error,
    );
    try {
      await ref.read(storyRepositoryProvider).markSeen(story.id);
    } on ApiException {
      // Seen here is enough; the server will hear next time.
    }
  }

  void added(Story story) {
    state = StoriesState(stories: [story, ...state.stories], loading: false);
  }

  Future<void> delete(String storyId) async {
    await ref.read(storyRepositoryProvider).delete(storyId);
    state = StoriesState(
      stories: [
        for (final s in state.stories)
          if (s.id != storyId) s,
      ],
      loading: false,
    );
  }
}
