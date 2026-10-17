import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../auth/auth_controller.dart';
import '../../feed/widgets/report_sheet.dart';
import '../chat_models.dart';
import '../conversation_controller.dart';
import 'chats_screen.dart';

/// One conversation: its messages, newest at the bottom, and a box to reply.
class ConversationScreen extends ConsumerStatefulWidget {
  const ConversationScreen({super.key, required this.conversationId});

  final String conversationId;

  @override
  ConsumerState<ConversationScreen> createState() => _ConversationScreenState();
}

class _ConversationScreenState extends ConsumerState<ConversationScreen> {
  final _text = TextEditingController();
  final _scroll = ScrollController();

  ConversationController get _controller =>
      ref.read(conversationControllerProvider(widget.conversationId).notifier);

  @override
  void initState() {
    super.initState();
    // The list runs bottom-up, so the far end is the oldest message.
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 400) _controller.loadEarlier();
    });
  }

  @override
  void dispose() {
    _text.dispose();
    _scroll.dispose();
    super.dispose();
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Runs [action], showing the server's message if it fails. True on success.
  Future<bool> _attempt(Future<void> Function() action) async {
    try {
      await action();
      return true;
    } on ApiException catch (error) {
      _toast(error.message);
      return false;
    }
  }

  void _send() {
    final body = _text.text.trim();
    if (body.isEmpty) return;
    _text.clear();
    _controller.sendText(body);
    if (_scroll.hasClients) _scroll.jumpTo(0);
  }

  Future<void> _sendPhoto() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      maxWidth: 2048,
      imageQuality: 85,
    );
    if (picked == null) return;
    final bytes = await picked.readAsBytes();
    await _controller.sendPhoto(bytes);
  }

  Future<bool> _confirm(String title, String body, String action) async {
    final answer = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: Text(title),
        content: Text(body),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Cancel')),
          TextButton(
            onPressed: () => Navigator.of(dialog).pop(true),
            style: TextButton.styleFrom(foregroundColor: Palette.danger),
            child: Text(action),
          ),
        ],
      ),
    );
    return answer ?? false;
  }

  void _messageMenu(Message message, {required bool mine}) {
    if (message.isSystem || message.deleted) return;
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (message.sendState == SendState.failed) ...[
              ListTile(
                leading: const Icon(Icons.refresh_rounded),
                title: const Text('Try again'),
                onTap: () {
                  Navigator.of(sheet).pop();
                  _controller.retry(message.localId!);
                },
              ),
              ListTile(
                leading: const Icon(Icons.delete_outline_rounded),
                title: const Text("Don't send"),
                onTap: () {
                  Navigator.of(sheet).pop();
                  _controller.discard(message.localId!);
                },
              ),
            ],
            if (message.body != null)
              ListTile(
                leading: const Icon(Icons.copy_rounded),
                title: const Text('Copy'),
                onTap: () {
                  Navigator.of(sheet).pop();
                  Clipboard.setData(ClipboardData(text: message.body!));
                },
              ),
            if (mine && !message.isPending)
              ListTile(
                leading: const Icon(Icons.delete_outline_rounded, color: Palette.danger),
                title: const Text('Delete for everyone'),
                onTap: () async {
                  Navigator.of(sheet).pop();
                  if (await _confirm('Delete this message?',
                      'It will say "Message deleted" for everyone in the chat.', 'Delete')) {
                    await _attempt(() => _controller.delete(message));
                  }
                },
              ),
            if (!mine)
              ListTile(
                leading: const Icon(Icons.flag_outlined),
                title: const Text('Report message'),
                onTap: () {
                  Navigator.of(sheet).pop();
                  showReportSheet(
                    context,
                    what: 'message',
                    send: (reason, details) => _controller.report(message, reason: reason, details: details),
                  );
                },
              ),
          ],
        ),
      ),
    );
  }

  Future<void> _menu(String choice, Conversation conversation) async {
    switch (choice) {
      case 'mute':
        await _attempt(() => _controller.setMuted(!conversation.muted));
      case 'block':
        final name = conversation.otherUser?.displayName ?? 'this person';
        if (await _confirm('Block $name?',
                "You won't see each other anywhere in MyRadius, and they can't message you.", 'Block') &&
            await _attempt(_controller.blockOther) &&
            mounted) {
          context.go(Routes.chats);
        }
      case 'leave':
        if (await _confirm('Leave ${conversation.name}?',
                "You won't get its messages any more. Someone would have to add you again.", 'Leave') &&
            await _attempt(_controller.leave) &&
            mounted) {
          context.go(Routes.chats);
        }
    }
  }

  Future<void> _blockAndReport() async {
    var done = false;
    await showReportSheet(
      context,
      what: 'request',
      send: (reason, details) async {
        await _controller.blockAndReport(reason: reason, details: details);
        done = true;
      },
    );
    if (done && mounted) context.go(Routes.chats);
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final state = ref.watch(conversationControllerProvider(widget.conversationId));
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    final conversation = state.conversation;

    return Scaffold(
      backgroundColor: const Color(0xFFF7EDE4),
      appBar: AppBar(
        backgroundColor: Palette.white,
        titleSpacing: 0,
        title: conversation == null
            ? null
            : Row(
                children: [
                  conversationAvatar(conversation, radius: 18),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(conversation.name,
                            maxLines: 1, overflow: TextOverflow.ellipsis, style: text.titleMedium),
                        if (conversation.isGroup || conversation.muted)
                          Text(
                            [
                              if (conversation.isGroup) '${conversation.memberCount} members',
                              if (conversation.muted) 'Muted',
                            ].join(' · '),
                            style: text.bodySmall?.copyWith(fontSize: 12.5),
                          ),
                      ],
                    ),
                  ),
                ],
              ),
        actions: [
          if (conversation != null && !conversation.awaitsMe)
            PopupMenuButton<String>(
              tooltip: 'More',
              onSelected: (choice) => _menu(choice, conversation),
              itemBuilder: (_) => [
                PopupMenuItem(value: 'mute', child: Text(conversation.muted ? 'Unmute' : 'Mute')),
                if (conversation.otherUser != null)
                  PopupMenuItem(value: 'block', child: Text('Block ${conversation.otherUser!.displayName}')),
                if (conversation.isGroup) const PopupMenuItem(value: 'leave', child: Text('Leave group')),
              ],
            ),
        ],
      ),
      body: Column(
        children: [
          if (conversation?.safetyNotice != null)
            Container(
              width: double.infinity,
              margin: const EdgeInsets.fromLTRB(12, 10, 12, 0),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(color: Palette.trustTint, borderRadius: BorderRadius.circular(12)),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Icon(Icons.shield_outlined, size: 18, color: Palette.trustInk),
                  const SizedBox(width: 8),
                  Expanded(
                    child: Text(conversation!.safetyNotice!,
                        style: text.bodySmall?.copyWith(color: Palette.trustInk)),
                  ),
                ],
              ),
            ),
          Expanded(child: _messages(state, myId)),
          if (conversation != null) _bottom(conversation, state, myId),
        ],
      ),
    );
  }

  Widget _messages(ConversationState state, String? myId) {
    final text = Theme.of(context).textTheme;
    if (state.loading && state.messages.isEmpty) {
      return const Center(child: CircularProgressIndicator(color: Palette.primary));
    }
    if (state.error != null && state.conversation == null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(
            state.error!.statusCode == 404 ? 'This conversation is no longer available.' : state.error!.message,
            textAlign: TextAlign.center,
            style: text.bodyLarge,
          ),
        ),
      );
    }
    final messages = state.messages;
    if (messages.isEmpty) {
      final other = state.conversation?.otherUser?.displayName;
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(
            other == null
                ? 'No messages yet.'
                : 'Say hello to $other. Until they reply, you can send one message.',
            textAlign: TextAlign.center,
            style: text.bodyMedium?.copyWith(color: Palette.muted),
          ),
        ),
      );
    }

    // The newest message of mine they have seen carries "Read".
    final seenSeq = messages.lastWhere((m) => _controller.seen(m), orElse: () => messages.first);
    final readMarker = _controller.seen(seenSeq) ? seenSeq.id : null;

    // Bottom-up: index 0 is the newest message.
    return ListView.builder(
      controller: _scroll,
      reverse: true,
      padding: const EdgeInsets.fromLTRB(12, 12, 12, 12),
      itemCount: messages.length + (state.hasEarlier ? 1 : 0),
      itemBuilder: (context, index) {
        if (index == messages.length) {
          return const Padding(
            padding: EdgeInsets.all(12),
            child: Center(child: CircularProgressIndicator(color: Palette.primary, strokeWidth: 2)),
          );
        }
        final message = messages[messages.length - 1 - index];
        final older = index + 1 < messages.length ? messages[messages.length - 2 - index] : null;
        final newDay = older == null || dayLabel(older.createdAt) != dayLabel(message.createdAt);
        final mine = message.sender?.id == myId;
        final showName = state.conversation?.isGroup == true &&
            !mine &&
            !message.isSystem &&
            (older == null || older.sender?.id != message.sender?.id || newDay);
        return Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            if (newDay) _DayChip(dayLabel(message.createdAt)),
            if (message.isSystem)
              _SystemLine(systemText(message.system, names: state.names))
            else
              _Bubble(
                message: message,
                mine: mine,
                senderName: showName ? message.sender?.displayName : null,
                read: message.id == readMarker,
                onLongPress: () => _messageMenu(message, mine: mine),
              ),
          ],
        );
      },
    );
  }

  Widget _bottom(Conversation conversation, ConversationState state, String? myId) {
    final text = Theme.of(context).textTheme;
    if (conversation.awaitsMe) {
      return _Panel(
        children: [
          Text(
            conversation.isInvitation
                ? "You've been invited to ${conversation.name}. Accept to see its messages from now on."
                : '${conversation.name} wants to message you. They won\'t know if you decline.',
            style: text.bodyMedium,
          ),
          const SizedBox(height: 10),
          Row(
            children: [
              Expanded(
                child: OutlinedButton(
                  onPressed: () async {
                    if (await _attempt(() => _controller.respond(accept: false)) && mounted) {
                      context.go(Routes.chats);
                    }
                  },
                  child: const Text('Decline'),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: FilledButton(
                  onPressed: () => _attempt(() => _controller.respond(accept: true)),
                  child: const Text('Accept'),
                ),
              ),
            ],
          ),
          if (conversation.isRequestReceived)
            TextButton.icon(
              onPressed: _blockAndReport,
              style: TextButton.styleFrom(foregroundColor: Palette.danger),
              icon: const Icon(Icons.block_rounded, size: 18),
              label: const Text('Block and report'),
            ),
        ],
      );
    }
    final waiting = conversation.isRequestSent && state.messages.any((m) => m.sender?.id == myId && !m.isPending);
    if (waiting) {
      return _Panel(
        children: [
          Text(
            'Waiting for ${conversation.name} to reply. You can send more once they do.',
            style: text.bodyMedium?.copyWith(color: Palette.muted),
            textAlign: TextAlign.center,
          ),
        ],
      );
    }
    return SafeArea(
      top: false,
      child: Container(
        padding: const EdgeInsets.fromLTRB(8, 8, 8, 8),
        decoration: const BoxDecoration(
          color: Palette.white,
          border: Border(top: BorderSide(color: Palette.line)),
        ),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.end,
          children: [
            IconButton(
              onPressed: _sendPhoto,
              tooltip: 'Send a photo',
              style: IconButton.styleFrom(backgroundColor: Palette.cream),
              icon: const Icon(Icons.image_outlined, color: Palette.ink),
            ),
            const SizedBox(width: 6),
            Expanded(
              child: TextField(
                controller: _text,
                minLines: 1,
                maxLines: 5,
                maxLength: 20000,
                textCapitalization: TextCapitalization.sentences,
                decoration: const InputDecoration(
                  hintText: 'Message',
                  counterText: '',
                  contentPadding: EdgeInsets.symmetric(horizontal: 16, vertical: 12),
                ),
              ),
            ),
            const SizedBox(width: 6),
            IconButton.filled(
              onPressed: _send,
              tooltip: 'Send',
              style: IconButton.styleFrom(backgroundColor: Palette.primary),
              icon: const Icon(Icons.arrow_forward_rounded, color: Palette.white),
            ),
          ],
        ),
      ),
    );
  }
}

class _Panel extends StatelessWidget {
  const _Panel({required this.children});

  final List<Widget> children;

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      top: false,
      child: Container(
        width: double.infinity,
        padding: const EdgeInsets.fromLTRB(16, 12, 16, 12),
        decoration: const BoxDecoration(
          color: Palette.white,
          border: Border(top: BorderSide(color: Palette.line)),
        ),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: children,
        ),
      ),
    );
  }
}

class _DayChip extends StatelessWidget {
  const _DayChip(this.label);

  final String label;

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        margin: const EdgeInsets.symmetric(vertical: 10),
        padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
        decoration: BoxDecoration(color: const Color(0xFFEFDFD2), borderRadius: BorderRadius.circular(10)),
        child: Text(label, style: const TextStyle(fontSize: 12, color: Palette.inkSoft)),
      ),
    );
  }
}

class _SystemLine extends StatelessWidget {
  const _SystemLine(this.text);

  final String text;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 6, horizontal: 24),
      child: Text(
        text,
        textAlign: TextAlign.center,
        style: Theme.of(context).textTheme.bodySmall?.copyWith(fontSize: 12.5),
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.message,
    required this.mine,
    required this.read,
    required this.onLongPress,
    this.senderName,
  });

  final Message message;
  final bool mine;
  final bool read;
  final String? senderName;
  final VoidCallback onLongPress;

  @override
  Widget build(BuildContext context) {
    final failed = message.sendState == SendState.failed;
    final ink = mine ? Palette.white : Palette.ink;
    final metaInk = mine ? const Color(0xFFF8D3C3) : Palette.muted;
    final meta = switch (message.sendState) {
      SendState.sending => 'Sending…',
      SendState.failed => 'Not sent · tap for options',
      SendState.sent => read ? '${clockTime(message.createdAt)} · Read' : clockTime(message.createdAt),
    };
    final width = (MediaQuery.sizeOf(context).width * 0.7 * MediaQuery.devicePixelRatioOf(context)).round();

    Widget content;
    if (message.deleted) {
      content = Text('Message deleted', style: TextStyle(color: metaInk, fontStyle: FontStyle.italic, fontSize: 15));
    } else if (message.localImage != null || message.image != null) {
      content = ClipRRect(
        borderRadius: BorderRadius.circular(10),
        child: ConstrainedBox(
          constraints: const BoxConstraints(maxHeight: 320),
          child: message.localImage != null
              ? Image.memory(Uint8List.fromList(message.localImage!), fit: BoxFit.cover)
              : CachedNetworkImage(
                  imageUrl: message.image!.urlFor(width),
                  fit: BoxFit.cover,
                  placeholder: (_, _) => const SizedBox(height: 200, child: ColoredBox(color: Palette.sand)),
                ),
        ),
      );
    } else {
      content = Text(message.body ?? '', style: TextStyle(color: ink, fontSize: 15, height: 1.35));
    }

    return Align(
      alignment: mine ? Alignment.centerRight : Alignment.centerLeft,
      child: ConstrainedBox(
        constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.78),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 3),
          child: Material(
            color: failed ? const Color(0xFFE8A28C) : (mine ? Palette.primary : Palette.white),
            borderRadius: BorderRadius.only(
              topLeft: const Radius.circular(16),
              topRight: const Radius.circular(16),
              bottomLeft: Radius.circular(mine ? 16 : 4),
              bottomRight: Radius.circular(mine ? 4 : 16),
            ),
            child: InkWell(
              borderRadius: BorderRadius.circular(16),
              onLongPress: onLongPress,
              onTap: failed ? onLongPress : null,
              child: Padding(
                padding: const EdgeInsets.fromLTRB(12, 8, 12, 6),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.end,
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    if (senderName != null)
                      Align(
                        alignment: Alignment.centerLeft,
                        child: Padding(
                          padding: const EdgeInsets.only(bottom: 2),
                          child: Text(senderName!,
                              style: const TextStyle(
                                  color: Palette.primaryDark, fontSize: 13, fontWeight: FontWeight.w600)),
                        ),
                      ),
                    Align(alignment: Alignment.centerLeft, widthFactor: 1, child: content),
                    const SizedBox(height: 3),
                    Text(meta, style: TextStyle(color: metaInk, fontSize: 11)),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
