import '../feed/feed_models.dart';

/// Whether a message of mine has reached the server.
enum SendState { sent, sending, failed }

class Message {
  const Message({
    required this.id,
    required this.conversationId,
    required this.seq,
    required this.kind,
    required this.createdAt,
    this.sender,
    this.body,
    this.image,
    this.system,
    this.deleted = false,
    this.sendState = SendState.sent,
    this.localId,
    this.idempotencyKey,
    this.error,
    this.localImage,
  });

  factory Message.fromJson(Map<String, dynamic> json) {
    final image = json['image'];
    return Message(
      id: json['id'] as String,
      conversationId: json['conversation_id'] as String,
      seq: json['seq'] as int,
      kind: json['kind'] as String,
      sender: json['sender'] == null
          ? null
          : Author.fromJson(Map<String, dynamic>.from(json['sender'] as Map)),
      body: json['body'] as String?,
      image: image is Map
          ? PostImage(
              mediaId: json['id'] as String,
              urlsByWidth: {
                for (final entry in image.entries)
                  int.parse('${entry.key}'): (entry.value as Map)['url'] as String,
              },
            )
          : null,
      system: json['system'] is Map ? Map<String, dynamic>.from(json['system'] as Map) : null,
      deleted: json['deleted'] as bool? ?? false,
      createdAt: DateTime.parse(json['created_at'] as String),
    );
  }

  /// A message being sent: it has no ID or sequence number until the server
  /// answers.
  factory Message.pending({
    required String localId,
    required String conversationId,
    required Author sender,
    required String idempotencyKey,
    String? body,
    List<int>? localImage,
  }) =>
      Message(
        id: '',
        conversationId: conversationId,
        seq: 0,
        kind: localImage == null ? 'text' : 'image',
        createdAt: DateTime.now(),
        sender: sender,
        body: body,
        sendState: SendState.sending,
        localId: localId,
        idempotencyKey: idempotencyKey,
        localImage: localImage,
      );

  final String id;
  final String conversationId;
  final int seq;

  /// `text`, `image` or `system`.
  final String kind;
  final Author? sender;
  final String? body;
  final PostImage? image;
  final Map<String, dynamic>? system;
  final bool deleted;
  final DateTime createdAt;

  final SendState sendState;
  final String? localId;
  final String? idempotencyKey;
  final String? error;

  /// The photo's bytes while it is still being sent, so it shows at once.
  final List<int>? localImage;

  bool get isSystem => kind == 'system';
  bool get isPending => sendState != SendState.sent;

  Message copyWith({bool? deleted, SendState? sendState, String? error}) => Message(
        id: id,
        conversationId: conversationId,
        seq: seq,
        kind: kind,
        createdAt: createdAt,
        sender: sender,
        body: deleted == true ? null : body,
        image: deleted == true ? null : image,
        system: system,
        deleted: deleted ?? this.deleted,
        sendState: sendState ?? this.sendState,
        localId: localId,
        idempotencyKey: idempotencyKey,
        error: error,
        localImage: localImage,
      );
}

class Conversation {
  const Conversation({
    required this.id,
    required this.kind,
    required this.state,
    required this.myRole,
    required this.myStatus,
    required this.muted,
    required this.memberCount,
    required this.lastSeq,
    required this.lastReadSeq,
    required this.unreadCount,
    required this.updatedAt,
    this.title,
    this.otherUser,
    this.otherLastReadSeq,
    this.safetyNotice,
    this.lastMessage,
    this.lastMessageAt,
  });

  factory Conversation.fromJson(Map<String, dynamic> json) => Conversation(
        id: json['id'] as String,
        kind: json['kind'] as String,
        title: json['title'] as String?,
        state: json['state'] as String,
        myRole: json['my_role'] as String,
        myStatus: json['my_status'] as String,
        muted: json['muted'] as bool? ?? false,
        otherUser: json['other_user'] == null
            ? null
            : Author.fromJson(Map<String, dynamic>.from(json['other_user'] as Map)),
        otherLastReadSeq: json['other_last_read_seq'] as int?,
        safetyNotice: json['safety_notice'] as String?,
        memberCount: json['member_count'] as int,
        lastSeq: json['last_seq'] as int,
        lastReadSeq: json['last_read_seq'] as int,
        unreadCount: json['unread_count'] as int,
        lastMessage: json['last_message'] == null
            ? null
            : Message.fromJson(Map<String, dynamic>.from(json['last_message'] as Map)),
        lastMessageAt:
            json['last_message_at'] == null ? null : DateTime.parse(json['last_message_at'] as String),
        updatedAt: DateTime.parse(json['updated_at'] as String),
      );

  final String id;

  /// `direct`, `group`, `listing` or `service`.
  final String kind;
  final String? title;

  /// From my side: `active`, `request_sent`, `request_received` or `declined`.
  final String state;
  final String myRole;

  /// `active`, `invited`, `left` or `removed`.
  final String myStatus;
  final bool muted;
  final Author? otherUser;
  final int? otherLastReadSeq;
  final String? safetyNotice;
  final int memberCount;
  final int lastSeq;
  final int lastReadSeq;
  final int unreadCount;
  final Message? lastMessage;
  final DateTime? lastMessageAt;
  final DateTime updatedAt;

  bool get isGroup => kind == 'group';
  bool get isInvitation => myStatus == 'invited';
  bool get isRequestReceived => state == 'request_received';
  bool get isRequestSent => state == 'request_sent';

  /// In the Requests folder: someone wants to talk, and I have not said yes.
  bool get awaitsMe => isInvitation || isRequestReceived;

  /// Gone from my lists: I left, was removed, or declined.
  bool get isClosed => myStatus == 'left' || myStatus == 'removed' || state == 'declined';

  String get name => title ?? otherUser?.displayName ?? 'Conversation';

  /// Sorting key for the inbox: most recently active first.
  DateTime get activeAt => lastMessageAt ?? updatedAt;

  Conversation copyWith({int? lastReadSeq, int? unreadCount, bool? muted, int? otherLastReadSeq}) =>
      Conversation(
        id: id,
        kind: kind,
        title: title,
        state: state,
        myRole: myRole,
        myStatus: myStatus,
        muted: muted ?? this.muted,
        otherUser: otherUser,
        otherLastReadSeq: otherLastReadSeq ?? this.otherLastReadSeq,
        safetyNotice: safetyNotice,
        memberCount: memberCount,
        lastSeq: lastSeq,
        lastReadSeq: lastReadSeq ?? this.lastReadSeq,
        unreadCount: unreadCount ?? this.unreadCount,
        lastMessage: lastMessage,
        lastMessageAt: lastMessageAt,
        updatedAt: updatedAt,
      );
}

class MessagePage {
  const MessagePage(this.items, {required this.hasMore});
  final List<Message> items;
  final bool hasMore;
}

/// A one-line preview of a message, for the inbox. [names] resolves user IDs
/// in system messages; [myId] turns my own messages into "You: …".
String messagePreview(Message message, {String? myId, Map<String, String> names = const {}}) {
  if (message.deleted) return 'Message deleted';
  if (message.isSystem) return systemText(message.system, names: names);
  final text = message.image != null || message.kind == 'image' ? 'Photo' : (message.body ?? '');
  if (myId != null && message.sender?.id == myId) return 'You: $text';
  return text;
}

/// A system message written out: "Otieno joined". Names come from the member
/// list, so they are always current; anyone not in it is "Someone".
String systemText(Map<String, dynamic>? system, {Map<String, String> names = const {}}) {
  if (system == null) return '';
  String who(Object? id) => names['$id'] ?? 'Someone';
  final actor = who(system['actor_id']);
  final users = [for (final id in (system['user_ids'] as List?) ?? const []) who(id)];
  return switch (system['event']) {
    'created' => system['title'] == null ? '$actor started the chat' : '$actor created "${system['title']}"',
    'members_added' => '$actor added ${_list(users)}',
    'member_joined' => '${who(system['user_id'])} joined',
    'member_left' => '${who(system['user_id'])} left',
    'member_removed' => '$actor removed ${who(system['user_id'])}',
    'renamed' => '$actor renamed the chat "${system['title']}"',
    'role_changed' => '$actor made ${who(system['user_id'])} ${_role(system['role'])}',
    _ => 'The chat was updated',
  };
}

String _role(Object? role) => switch (role) {
      'owner' => 'the owner',
      'admin' => 'an admin',
      _ => 'a member',
    };

String _list(List<String> names) {
  if (names.isEmpty) return 'people';
  if (names.length == 1) return names.single;
  if (names.length == 2) return '${names[0]} and ${names[1]}';
  return '${names[0]}, ${names[1]} and ${names.length - 2} more';
}

/// "Today", "Yesterday", a weekday within the week, else the date.
String dayLabel(DateTime moment, {DateTime? now}) {
  final local = moment.toLocal();
  final today = (now ?? DateTime.now()).toLocal();
  final day = DateTime(local.year, local.month, local.day);
  final days = DateTime(today.year, today.month, today.day).difference(day).inDays;
  if (days == 0) return 'Today';
  if (days == 1) return 'Yesterday';
  const weekdays = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
  if (days < 7) return weekdays[local.weekday - 1];
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  final year = local.year == today.year ? '' : ' ${local.year}';
  return '${local.day} ${months[local.month - 1]}$year';
}

/// "14:05", the time a message was sent.
String clockTime(DateTime moment) {
  final local = moment.toLocal();
  return '${local.hour.toString().padLeft(2, '0')}:${local.minute.toString().padLeft(2, '0')}';
}
