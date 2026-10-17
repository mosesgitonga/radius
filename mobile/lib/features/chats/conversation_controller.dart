import 'dart:async';
import 'dart:typed_data';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:uuid/uuid.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/realtime.dart';
import '../auth/auth_controller.dart';
import '../feed/feed_models.dart';
import 'chat_models.dart';
import 'chats_controller.dart';

class ConversationState {
  const ConversationState({
    this.conversation,
    this.messages = const [],
    this.names = const {},
    this.hasEarlier = false,
    this.loading = true,
    this.loadingEarlier = false,
    this.error,
  });

  final Conversation? conversation;

  /// Sent messages in sequence order, then any still sending.
  final List<Message> messages;

  /// Members' names by ID, for system messages.
  final Map<String, String> names;
  final bool hasEarlier;
  final bool loading;
  final bool loadingEarlier;
  final ApiException? error;

  /// The highest sequence number held: catching up fetches after it.
  int get lastSeq => messages.where((m) => !m.isPending).fold(0, (a, m) => m.seq > a ? m.seq : a);

  ConversationState copyWith({
    Conversation? conversation,
    List<Message>? messages,
    Map<String, String>? names,
    bool? hasEarlier,
    bool? loading,
    bool? loadingEarlier,
    ApiException? error,
    bool clearError = false,
  }) =>
      ConversationState(
        conversation: conversation ?? this.conversation,
        messages: messages ?? this.messages,
        names: names ?? this.names,
        hasEarlier: hasEarlier ?? this.hasEarlier,
        loading: loading ?? this.loading,
        loadingEarlier: loadingEarlier ?? this.loadingEarlier,
        error: clearError ? null : (error ?? this.error),
      );
}

final conversationControllerProvider =
    NotifierProvider.autoDispose.family<ConversationController, ConversationState, String>(
  ConversationController.new,
);

/// One open conversation. Lives while its screen does.
class ConversationController extends Notifier<ConversationState> {
  ConversationController(this.id);

  final String id;
  bool _catchingUp = false;
  bool _catchUpAgain = false;
  bool _disposed = false;

  @override
  ConversationState build() {
    final subscription = ref.read(realtimeProvider).events.listen(_onEvent);
    ref.onDispose(() {
      _disposed = true;
      subscription.cancel();
    });
    final known = ref.read(chatsControllerProvider);
    final cached = [...known.chats, ...known.requests].where((c) => c.id == id).firstOrNull;
    Future.microtask(load);
    return ConversationState(conversation: cached);
  }

  String? get _myId {
    final auth = ref.read(authControllerProvider);
    return auth is AuthSignedIn ? auth.me.id : null;
  }

  Future<void> load() async {
    final repo = ref.read(chatRepositoryProvider);
    try {
      final conversation = await repo.conversation(id);
      final page = await repo.messages(id);
      final names = conversation.isGroup ? await repo.memberNames(id) : <String, String>{};
      if (_disposed) return;
      state = ConversationState(
        conversation: conversation,
        messages: [...page.items, ...state.messages.where((m) => m.isPending)],
        names: names,
        hasEarlier: page.hasMore,
        loading: false,
      );
      await markRead();
    } on ApiException catch (error) {
      if (!_disposed) state = state.copyWith(loading: false, error: error);
    }
  }

  void _onEvent(RealtimeEvent event) {
    if (event.type == RealtimeEvent.connected) {
      _catchUp();
      _reloadConversation();
      return;
    }
    if (event.conversationId != id) return;
    switch (event.type) {
      case 'message.created':
        _catchUp();
      case 'message.deleted':
        final messageId = event.data['message_id'];
        state = state.copyWith(messages: [
          for (final m in state.messages) m.id == messageId ? m.copyWith(deleted: true) : m,
        ]);
      case 'conversation.read' || 'conversation.updated':
        _reloadConversation();
    }
  }

  /// Fetches everything after the last message held. Events that arrive while
  /// a catch-up runs ask for one more afterwards.
  Future<void> _catchUp() async {
    if (state.loading) return;
    if (_catchingUp) {
      _catchUpAgain = true;
      return;
    }
    _catchingUp = true;
    try {
      final repo = ref.read(chatRepositoryProvider);
      bool more;
      do {
        final page = await repo.messages(id, afterSeq: state.lastSeq, limit: 100);
        if (_disposed) return;
        _merge(page.items);
        more = page.hasMore && page.items.isNotEmpty;
      } while (more);
      await markRead();
    } on ApiException {
      // The next event or reconnect tries again.
    } finally {
      _catchingUp = false;
      if (_catchUpAgain && !_disposed) {
        _catchUpAgain = false;
        unawaited(_catchUp());
      }
    }
  }

  Future<void> _reloadConversation() async {
    try {
      final conversation = await ref.read(chatRepositoryProvider).conversation(id);
      if (!_disposed) state = state.copyWith(conversation: conversation);
    } on ApiException {
      // Shown as it was; the next event tries again.
    }
  }

  Future<void> loadEarlier() async {
    final sent = state.messages.where((m) => !m.isPending);
    if (!state.hasEarlier || state.loadingEarlier || sent.isEmpty) return;
    state = state.copyWith(loadingEarlier: true);
    try {
      final page = await ref.read(chatRepositoryProvider).messages(id, beforeSeq: sent.first.seq);
      if (_disposed) return;
      _merge(page.items);
      state = state.copyWith(hasEarlier: page.hasMore, loadingEarlier: false);
    } on ApiException catch (error) {
      if (!_disposed) state = state.copyWith(loadingEarlier: false, error: error);
    }
  }

  /// Adds messages from the server, each once, in sequence order.
  void _merge(List<Message> incoming) {
    final byId = {for (final m in state.messages.where((m) => !m.isPending)) m.id: m};
    for (final m in incoming) {
      byId[m.id] = m;
    }
    final sent = byId.values.toList()..sort((a, b) => a.seq.compareTo(b.seq));
    state = state.copyWith(messages: [...sent, ...state.messages.where((m) => m.isPending)]);
  }

  /// Marks the conversation read up to the newest message, if it is not already.
  Future<void> markRead() async {
    final conversation = state.conversation;
    final seq = state.lastSeq;
    if (conversation == null || seq <= conversation.lastReadSeq) return;
    state = state.copyWith(conversation: conversation.copyWith(lastReadSeq: seq, unreadCount: 0));
    ref.read(chatsControllerProvider.notifier).markedRead(id, seq);
    try {
      await ref.read(chatRepositoryProvider).markRead(id, seq);
    } on ApiException {
      // Harmless: the next read mark covers it.
    }
  }

  /// Sends text. Shown at once; on failure it stays, marked, to retry.
  Future<void> sendText(String body) => _send(body: body.trim());

  /// Sends one photo: uploaded, processed, then sent as a message.
  Future<void> sendPhoto(Uint8List bytes) => _send(photo: bytes);

  Future<void> retry(String localId) async {
    final message = state.messages.where((m) => m.localId == localId).firstOrNull;
    if (message == null || message.sendState != SendState.failed) return;
    _replacePending(localId, message.copyWith(sendState: SendState.sending));
    await _deliver(message.localId!, message.idempotencyKey!, body: message.body,
        photo: message.localImage == null ? null : Uint8List.fromList(message.localImage!));
  }

  void discard(String localId) =>
      state = state.copyWith(messages: state.messages.where((m) => m.localId != localId).toList());

  Future<void> _send({String? body, Uint8List? photo}) async {
    final auth = ref.read(authControllerProvider);
    if (auth is! AuthSignedIn) return;
    if ((body == null || body.isEmpty) && photo == null) return;
    const uuid = Uuid();
    final localId = uuid.v4();
    final idempotencyKey = uuid.v4();
    final me = auth.me;
    state = state.copyWith(messages: [
      ...state.messages,
      Message.pending(
        localId: localId,
        conversationId: id,
        sender: Author(id: me.id, displayName: me.displayName, isVerifiedNeighbour: me.isVerifiedNeighbour),
        idempotencyKey: idempotencyKey,
        body: body,
        localImage: photo,
      ),
    ]);
    await _deliver(localId, idempotencyKey, body: body, photo: photo);
  }

  /// The photo is uploaded again on a retry: an upload that failed half way
  /// cannot be resumed, and one that finished is cheap to repeat.
  Future<void> _deliver(String localId, String idempotencyKey, {String? body, Uint8List? photo}) async {
    try {
      final mediaId = photo == null
          ? null
          : await ref.read(mediaUploaderProvider).uploadPhoto(photo, purpose: 'message');
      final sent = await ref.read(chatRepositoryProvider).send(
            id,
            idempotencyKey: idempotencyKey,
            body: photo == null ? body : null,
            mediaId: mediaId,
          );
      if (_disposed) return;
      state = state.copyWith(messages: state.messages.where((m) => m.localId != localId).toList());
      _merge([sent]);
      if (state.conversation?.state == 'request_sent' || state.conversation?.state == 'request_received') {
        unawaited(_reloadConversation());
      }
      unawaited(markRead());
    } on ApiException catch (error) {
      if (_disposed) return;
      final pending = state.messages.where((m) => m.localId == localId).firstOrNull;
      if (pending != null) {
        _replacePending(localId, pending.copyWith(sendState: SendState.failed, error: error.message));
      }
      if (error.details['reason'] == 'awaiting_reply') unawaited(_reloadConversation());
    }
  }

  void _replacePending(String localId, Message message) => state = state.copyWith(messages: [
        for (final m in state.messages) m.localId == localId ? message : m,
      ]);

  /// Deletes my message for everyone.
  Future<void> delete(Message message) async {
    await ref.read(chatRepositoryProvider).deleteMessage(message.id);
    state = state.copyWith(messages: [
      for (final m in state.messages) m.id == message.id ? m.copyWith(deleted: true) : m,
    ]);
  }

  Future<void> report(Message message, {required String reason, String? details}) =>
      ref.read(feedRepositoryProvider).report(
            targetType: 'message',
            targetId: message.id,
            reason: reason,
            details: details,
          );

  /// Accepts or declines a request or an invitation.
  Future<void> respond({required bool accept}) async {
    final conversation = state.conversation;
    if (conversation == null) return;
    final repo = ref.read(chatRepositoryProvider);
    final updated = conversation.isInvitation
        ? await repo.respondToInvitation(id, accept: accept)
        : await repo.respondToRequest(id, accept: accept);
    state = state.copyWith(conversation: updated);
    ref.read(chatsControllerProvider.notifier).put(updated);
    if (accept && updated.isGroup) {
      // An invitee sees nothing until they accept.
      await load();
    }
  }

  Future<void> blockAndReport({required String reason, String? details}) async {
    await ref.read(chatRepositoryProvider).blockAndReport(id, reason: reason, details: details);
    ref.read(chatsControllerProvider.notifier).remove(id);
  }

  Future<void> setMuted(bool muted) async {
    await ref.read(chatRepositoryProvider).setMuted(id, muted);
    final updated = state.conversation?.copyWith(muted: muted);
    if (updated != null) {
      state = state.copyWith(conversation: updated);
      ref.read(chatsControllerProvider.notifier).put(updated);
    }
  }

  Future<void> leave() async {
    await ref.read(chatRepositoryProvider).leave(id);
    ref.read(chatsControllerProvider.notifier).remove(id);
  }

  Future<void> blockOther() async {
    final other = state.conversation?.otherUser;
    if (other == null) return;
    await ref.read(chatRepositoryProvider).block(other.id);
    ref.read(chatsControllerProvider.notifier).remove(id);
  }

  /// Whether my message [message] has been read by the other person: direct
  /// conversations only, and only once accepted.
  bool seen(Message message) {
    final other = state.conversation?.otherLastReadSeq;
    return other != null && !message.isPending && message.sender?.id == _myId && message.seq <= other;
  }
}
