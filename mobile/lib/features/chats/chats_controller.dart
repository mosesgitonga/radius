import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/realtime.dart';
import 'chat_models.dart';

class ChatsState {
  const ChatsState({
    this.chats = const [],
    this.requests = const [],
    this.nextCursor,
    this.loading = true,
    this.loadingMore = false,
    this.error,
  });

  /// The inbox, most recently active first.
  final List<Conversation> chats;

  /// Message requests and group invitations waiting on me.
  final List<Conversation> requests;
  final String? nextCursor;
  final bool loading;
  final bool loadingMore;
  final ApiException? error;

  int get unread => chats.where((c) => !c.muted).fold(0, (sum, c) => sum + c.unreadCount);

  ChatsState copyWith({
    List<Conversation>? chats,
    List<Conversation>? requests,
    String? nextCursor,
    bool clearCursor = false,
    bool? loading,
    bool? loadingMore,
    ApiException? error,
    bool clearError = false,
  }) =>
      ChatsState(
        chats: chats ?? this.chats,
        requests: requests ?? this.requests,
        nextCursor: clearCursor ? null : (nextCursor ?? this.nextCursor),
        loading: loading ?? this.loading,
        loadingMore: loadingMore ?? this.loadingMore,
        error: clearError ? null : (error ?? this.error),
      );
}

/// Discarded with the signed-in shell, so the next account starts empty.
final chatsControllerProvider =
    NotifierProvider.autoDispose<ChatsController, ChatsState>(ChatsController.new);

/// The Chats tab: the inbox and the requests, kept current by the socket.
/// Alive for as long as the signed-in shell, which shows its unread count.
class ChatsController extends Notifier<ChatsState> {
  /// Conversations being re-fetched, so a burst of events costs one request.
  final _inFlight = <String>{};

  @override
  ChatsState build() {
    final subscription = ref.read(realtimeProvider).events.listen(_onEvent);
    ref.onDispose(subscription.cancel);
    Future.microtask(refresh);
    return const ChatsState();
  }

  Future<void> refresh() async {
    state = state.copyWith(loading: true, clearError: true);
    final repo = ref.read(chatRepositoryProvider);
    try {
      final results = await Future.wait([
        repo.conversations(),
        repo.conversations(folder: 'requests'),
        repo.conversations(folder: 'invitations'),
      ]);
      state = ChatsState(
        chats: results[0].items,
        nextCursor: results[0].nextCursor,
        requests: _byActivity([...results[1].items, ...results[2].items]),
        loading: false,
      );
    } on ApiException catch (error) {
      state = state.copyWith(loading: false, error: error);
    }
  }

  Future<void> loadMore() async {
    if (state.nextCursor == null || state.loadingMore || state.loading) return;
    state = state.copyWith(loadingMore: true);
    try {
      final page = await ref.read(chatRepositoryProvider).conversations(cursor: state.nextCursor);
      final seen = {for (final c in state.chats) c.id};
      state = state.copyWith(
        chats: [...state.chats, ...page.items.where((c) => !seen.contains(c.id))],
        nextCursor: page.nextCursor,
        clearCursor: page.nextCursor == null,
        loadingMore: false,
      );
    } on ApiException catch (error) {
      state = state.copyWith(loadingMore: false, error: error);
    }
  }

  void _onEvent(RealtimeEvent event) {
    if (event.type == RealtimeEvent.connected) {
      // Back after a gap: anything may have changed.
      if (!state.loading) refresh();
      return;
    }
    final id = event.conversationId;
    if (id != null) reload(id);
  }

  /// Fetches one conversation again and files it where it now belongs.
  Future<void> reload(String id) async {
    if (!_inFlight.add(id)) return;
    try {
      put(await ref.read(chatRepositoryProvider).conversation(id));
    } on ApiException catch (error) {
      // Gone, or blocked either way: it leaves the lists.
      if (error.statusCode == 404) remove(id);
    } finally {
      _inFlight.remove(id);
    }
  }

  /// Files [conversation] in the inbox, the requests, or neither.
  void put(Conversation conversation) {
    final chats = state.chats.where((c) => c.id != conversation.id).toList();
    final requests = state.requests.where((c) => c.id != conversation.id).toList();
    if (!conversation.isClosed) {
      (conversation.awaitsMe ? requests : chats).add(conversation);
    }
    state = state.copyWith(chats: _byActivity(chats), requests: _byActivity(requests));
  }

  void remove(String id) => state = state.copyWith(
        chats: state.chats.where((c) => c.id != id).toList(),
        requests: state.requests.where((c) => c.id != id).toList(),
      );

  /// I have read up to [seq]: clear its unread count at once.
  void markedRead(String id, int seq) {
    state = state.copyWith(chats: [
      for (final c in state.chats)
        c.id == id && seq >= c.lastReadSeq ? c.copyWith(lastReadSeq: seq, unreadCount: 0) : c,
    ]);
  }

  static List<Conversation> _byActivity(List<Conversation> list) =>
      list..sort((a, b) => b.activeAt.compareTo(a.activeAt));
}
