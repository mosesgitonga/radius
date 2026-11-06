import '../../core/api/api_client.dart';
import '../../core/offline_store.dart';
import '../feed/feed_models.dart';
import 'chat_models.dart';

/// Conversations, messages, requests and blocking.
class ChatRepository {
  ChatRepository(this._api, [OfflineStore? offline]) : _offline = offline ?? MemoryOfflineStore();

  /// Kept offline: this many chats, each with its latest [savedMessages].
  static const savedChats = 30;
  static const savedMessages = 50;

  final ApiClient _api;
  final OfflineStore _offline;

  /// [folder] is `chats`, `requests` or `invitations`.
  Future<ResultPage<Conversation>> conversations({String folder = 'chats', String? cursor}) async {
    Future<dynamic> fetch() => _api.get('/conversations', query: {'folder': folder, 'cursor': ?cursor});
    final data = Map<String, dynamic>.from(
      cursor != null
          ? await fetch() as Map
          : await fetchOrRecall(_offline, 'chats_$folder', fetch, keep: (json) {
              final items = ((json as Map)['items'] as List).take(savedChats).toList();
              if (folder == 'chats') _pruneTo(items);
              return {'items': items, 'next_cursor': null};
            }) as Map,
    );
    return ResultPage(
      [
        for (final item in data['items'] as List)
          Conversation.fromJson(Map<String, dynamic>.from(item as Map)),
      ],
      data['next_cursor'] as String?,
    );
  }

  Future<Conversation> conversation(String id) async => Conversation.fromJson(
        Map<String, dynamic>.from(
          await fetchOrRecall(_offline, 'conversation_$id', () => _api.get('/conversations/$id')) as Map,
        ),
      );

  /// Saved chats outside the latest [savedChats] are forgotten.
  void _pruneTo(List<dynamic> items) {
    final ids = {for (final item in items) (item as Map)['id'] as String};
    _offline.retain('conversation_', {for (final id in ids) 'conversation_$id'});
    _offline.retain('messages_', {for (final id in ids) 'messages_$id'});
    _offline.retain('members_', {for (final id in ids) 'members_$id'});
  }

  /// The one direct conversation with this person, made if need be. Sends
  /// nothing.
  Future<Conversation> openDirect(String userId) async => Conversation.fromJson(Map<String, dynamic>.from(
      await _api.post('/conversations/direct', body: {'user_id': userId}) as Map));

  /// The latest page with no arguments; [afterSeq] catches up; [beforeSeq]
  /// scrolls back. Oldest first.
  Future<MessagePage> messages(String conversationId, {int? afterSeq, int? beforeSeq, int limit = 50}) async {
    Future<dynamic> fetch() => _api.get('/conversations/$conversationId/messages', query: {
      'after_seq': ?afterSeq,
      'before_seq': ?beforeSeq,
      'limit': limit,
    });
    // Only the latest page is kept; catching up and scrolling back need the server.
    final latest = afterSeq == null && beforeSeq == null;
    final data = Map<String, dynamic>.from(
      latest
          ? await fetchOrRecall(_offline, 'messages_$conversationId', fetch, keep: (json) {
              final items = (json as Map)['items'] as List;
              final start = items.length > savedMessages ? items.length - savedMessages : 0;
              return {'items': items.sublist(start), 'has_more': false};
            }) as Map
          : await fetch() as Map,
    );
    return MessagePage(
      [for (final item in data['items'] as List) Message.fromJson(Map<String, dynamic>.from(item as Map))],
      hasMore: data['has_more'] as bool,
    );
  }

  /// Members' names by user ID, for writing out system messages.
  Future<Map<String, String>> memberNames(String conversationId) async {
    final data = await fetchOrRecall(
      _offline,
      'members_$conversationId',
      () => _api.get('/conversations/$conversationId/members'),
    ) as List;
    return {
      for (final item in data)
        ((item as Map)['user'] as Map)['id'] as String: (item['user'] as Map)['display_name'] as String,
    };
  }

  /// [idempotencyKey] is the same on every attempt at one message.
  Future<Message> send(
    String conversationId, {
    required String idempotencyKey,
    String? body,
    String? mediaId,
  }) async =>
      Message.fromJson(Map<String, dynamic>.from(await _api.post(
        '/conversations/$conversationId/messages',
        body: {'body': ?body, 'media_id': ?mediaId},
        idempotencyKey: idempotencyKey,
      ) as Map));

  Future<void> markRead(String conversationId, int seq) =>
      _api.post('/conversations/$conversationId/read', body: {'seq': seq});

  Future<void> deleteMessage(String messageId) => _api.delete('/messages/$messageId');

  Future<Conversation> respondToRequest(String conversationId, {required bool accept}) async =>
      Conversation.fromJson(Map<String, dynamic>.from(await _api.post(
          '/conversations/$conversationId/request',
          body: {'action': accept ? 'accept' : 'decline'}) as Map));

  Future<Conversation> respondToInvitation(String conversationId, {required bool accept}) async =>
      Conversation.fromJson(Map<String, dynamic>.from(await _api.post(
          '/conversations/$conversationId/invitation',
          body: {'action': accept ? 'accept' : 'decline'}) as Map));

  /// Declines a request, reports its first message, and blocks the sender.
  Future<void> blockAndReport(String conversationId, {required String reason, String? details}) =>
      _api.post('/conversations/$conversationId/block-and-report', body: {
        'reason': reason,
        if (details != null && details.trim().isNotEmpty) 'details': details.trim(),
      });

  Future<void> setMuted(String conversationId, bool muted) =>
      _api.put('/conversations/$conversationId/mute', body: {'muted': muted});

  Future<void> leave(String conversationId) => _api.post('/conversations/$conversationId/leave');

  Future<void> block(String userId) => _api.post('/me/blocks', body: {'user_id': userId});
}
