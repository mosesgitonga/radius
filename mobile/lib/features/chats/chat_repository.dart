import '../../core/api/api_client.dart';
import '../feed/feed_models.dart';
import 'chat_models.dart';

/// Conversations, messages, requests and blocking.
class ChatRepository {
  ChatRepository(this._api);

  final ApiClient _api;

  /// [folder] is `chats`, `requests` or `invitations`.
  Future<ResultPage<Conversation>> conversations({String folder = 'chats', String? cursor}) async {
    final data = Map<String, dynamic>.from(await _api.get('/conversations',
        query: {'folder': folder, 'cursor': ?cursor}) as Map);
    return ResultPage(
      [
        for (final item in data['items'] as List)
          Conversation.fromJson(Map<String, dynamic>.from(item as Map)),
      ],
      data['next_cursor'] as String?,
    );
  }

  Future<Conversation> conversation(String id) async =>
      Conversation.fromJson(Map<String, dynamic>.from(await _api.get('/conversations/$id') as Map));

  /// The one direct conversation with this person, made if need be. Sends
  /// nothing.
  Future<Conversation> openDirect(String userId) async => Conversation.fromJson(Map<String, dynamic>.from(
      await _api.post('/conversations/direct', body: {'user_id': userId}) as Map));

  /// The latest page with no arguments; [afterSeq] catches up; [beforeSeq]
  /// scrolls back. Oldest first.
  Future<MessagePage> messages(String conversationId, {int? afterSeq, int? beforeSeq, int limit = 50}) async {
    final data = Map<String, dynamic>.from(await _api.get('/conversations/$conversationId/messages', query: {
      'after_seq': ?afterSeq,
      'before_seq': ?beforeSeq,
      'limit': limit,
    }) as Map);
    return MessagePage(
      [for (final item in data['items'] as List) Message.fromJson(Map<String, dynamic>.from(item as Map))],
      hasMore: data['has_more'] as bool,
    );
  }

  /// Members' names by user ID, for writing out system messages.
  Future<Map<String, String>> memberNames(String conversationId) async {
    final data = await _api.get('/conversations/$conversationId/members') as List;
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
