import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../feed/feed_models.dart';
import '../../feed/widgets/post_card.dart';
import '../chat_models.dart';
import '../chats_controller.dart';

/// The Chats tab: the inbox, and the requests waiting on me.
class ChatsScreen extends ConsumerStatefulWidget {
  const ChatsScreen({super.key});

  @override
  ConsumerState<ChatsScreen> createState() => _ChatsScreenState();
}

class _ChatsScreenState extends ConsumerState<ChatsScreen> {
  bool _showRequests = false;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final chats = ref.watch(chatsControllerProvider);
    final requests = chats.requests.length;

    return Scaffold(
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(16, 18, 16, 12),
              child: Text('Chats', style: text.headlineSmall),
            ),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 16),
              child: SegmentedButton<bool>(
                showSelectedIcon: false,
                segments: [
                  const ButtonSegment(value: false, label: Text('Chats')),
                  ButtonSegment(value: true, label: Text(requests == 0 ? 'Requests' : 'Requests · $requests')),
                ],
                selected: {_showRequests},
                onSelectionChanged: (value) => setState(() => _showRequests = value.single),
              ),
            ),
            const SizedBox(height: 8),
            Expanded(
              child: RefreshIndicator(
                color: Palette.primary,
                onRefresh: () => ref.read(chatsControllerProvider.notifier).refresh(),
                child: _showRequests
                    ? _Requests(chats: chats, onAccepted: () => setState(() => _showRequests = false))
                    : _Inbox(chats: chats),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Inbox extends ConsumerWidget {
  const _Inbox({required this.chats});

  final ChatsState chats;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final auth = ref.watch(authControllerProvider);
    final myId = auth is AuthSignedIn ? auth.me.id : null;
    if (chats.loading && chats.chats.isEmpty) {
      return const Center(child: CircularProgressIndicator(color: Palette.primary));
    }
    if (chats.error != null && chats.chats.isEmpty) {
      return _Empty(
        title: "Couldn't load your chats",
        body: chats.error!.message,
        action: 'Try again',
        onAction: () => ref.read(chatsControllerProvider.notifier).refresh(),
      );
    }
    if (chats.chats.isEmpty) {
      return const _Empty(
        title: 'No chats yet',
        body: 'Message a neighbour from one of their posts. Your conversations will be here.',
      );
    }
    return NotificationListener<ScrollNotification>(
      onNotification: (n) {
        if (n.metrics.pixels > n.metrics.maxScrollExtent - 400) {
          ref.read(chatsControllerProvider.notifier).loadMore();
        }
        return false;
      },
      child: ListView.builder(
        physics: const AlwaysScrollableScrollPhysics(),
        itemCount: chats.chats.length,
        itemBuilder: (context, index) {
          final conversation = chats.chats[index];
          return _ChatRow(
            conversation: conversation,
            myId: myId,
            onTap: () => context.go(Routes.conversation(conversation.id)),
          );
        },
      ),
    );
  }
}

/// An avatar for a conversation: the other person's, or the group's initials.
Widget conversationAvatar(Conversation conversation, {double radius = 26}) => InitialsAvatar(
      radius: radius,
      author: conversation.otherUser ??
          Author(id: conversation.id, displayName: conversation.name, isVerifiedNeighbour: false),
    );

class _ChatRow extends StatelessWidget {
  const _ChatRow({required this.conversation, required this.myId, required this.onTap});

  final Conversation conversation;
  final String? myId;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final unread = conversation.unreadCount;
    final last = conversation.lastMessage;
    final preview = last == null
        ? (conversation.isRequestSent ? 'Request sent' : 'No messages yet')
        : messagePreview(last, myId: myId);
    return InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 10),
        child: Row(
          children: [
            conversationAvatar(conversation),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          conversation.name,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: text.titleSmall?.copyWith(fontSize: 15.5),
                        ),
                      ),
                      if (conversation.muted)
                        const Padding(
                          padding: EdgeInsets.only(right: 4),
                          child: Icon(Icons.notifications_off_outlined, size: 15, color: Palette.muted),
                        ),
                      Text(
                        timeAgo(conversation.activeAt),
                        style: text.bodySmall?.copyWith(
                          fontSize: 12.5,
                          color: unread > 0 ? Palette.primary : Palette.muted,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 3),
                  Row(
                    children: [
                      Expanded(
                        child: Text(
                          preview,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: text.bodyMedium?.copyWith(
                            fontSize: 14,
                            color: unread > 0 ? Palette.ink : Palette.muted,
                            fontStyle: last?.deleted == true ? FontStyle.italic : null,
                          ),
                        ),
                      ),
                      if (unread > 0) ...[
                        const SizedBox(width: 8),
                        _Count(unread, muted: conversation.muted),
                      ],
                    ],
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}

class _Count extends StatelessWidget {
  const _Count(this.count, {this.muted = false});

  final int count;
  final bool muted;

  @override
  Widget build(BuildContext context) {
    return Container(
      constraints: const BoxConstraints(minWidth: 22),
      height: 22,
      padding: const EdgeInsets.symmetric(horizontal: 6),
      alignment: Alignment.center,
      decoration: BoxDecoration(
        color: muted ? Palette.lineStrong : Palette.primary,
        borderRadius: BorderRadius.circular(11),
      ),
      child: Text(
        count > 99 ? '99+' : '$count',
        style: const TextStyle(color: Palette.white, fontSize: 12, fontWeight: FontWeight.w600),
      ),
    );
  }
}

class _Requests extends ConsumerWidget {
  const _Requests({required this.chats, required this.onAccepted});

  final ChatsState chats;

  /// Back to the inbox, where the accepted conversation now is.
  final VoidCallback onAccepted;

  Future<void> _respond(BuildContext context, WidgetRef ref, Conversation c, bool accept) async {
    final repo = ref.read(chatRepositoryProvider);
    try {
      final updated = c.isInvitation
          ? await repo.respondToInvitation(c.id, accept: accept)
          : await repo.respondToRequest(c.id, accept: accept);
      ref.read(chatsControllerProvider.notifier).put(updated);
      if (accept && context.mounted) {
        onAccepted();
        context.go(Routes.conversation(c.id));
      }
    } on ApiException catch (error) {
      if (context.mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = Theme.of(context).textTheme;
    if (chats.loading && chats.requests.isEmpty) {
      return const Center(child: CircularProgressIndicator(color: Palette.primary));
    }
    return ListView(
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
      children: [
        Text(
          "People you haven't talked to yet, and group chats you've been invited to. "
          'Someone new can send one message until you reply.',
          style: text.bodyMedium?.copyWith(color: Palette.muted, fontSize: 13.5),
        ),
        const SizedBox(height: 12),
        if (chats.requests.isEmpty)
          Padding(
            padding: const EdgeInsets.only(top: 48),
            child: Text('No requests', textAlign: TextAlign.center, style: text.titleMedium),
          ),
        for (final c in chats.requests)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: Material(
              color: Palette.white,
              shape: RoundedRectangleBorder(
                borderRadius: BorderRadius.circular(14),
                side: const BorderSide(color: Palette.line),
              ),
              child: InkWell(
                borderRadius: BorderRadius.circular(14),
                onTap: () => context.go(Routes.conversation(c.id)),
                child: Padding(
                  padding: const EdgeInsets.all(14),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      Row(
                        children: [
                          conversationAvatar(c, radius: 20),
                          const SizedBox(width: 10),
                          Expanded(
                            child: Column(
                              crossAxisAlignment: CrossAxisAlignment.start,
                              children: [
                                Text(c.name, style: text.titleSmall),
                                Text(
                                  c.isInvitation
                                      ? 'Invited you to a group chat · ${c.memberCount} members'
                                      : timeAgo(c.activeAt),
                                  style: text.bodySmall?.copyWith(fontSize: 12.5),
                                ),
                              ],
                            ),
                          ),
                        ],
                      ),
                      if (!c.isInvitation && c.lastMessage != null) ...[
                        const SizedBox(height: 10),
                        Text(messagePreview(c.lastMessage!), style: text.bodyMedium),
                      ],
                      const SizedBox(height: 12),
                      Row(
                        children: [
                          Expanded(
                            child: OutlinedButton(
                              onPressed: () => _respond(context, ref, c, false),
                              child: const Text('Decline'),
                            ),
                          ),
                          const SizedBox(width: 8),
                          Expanded(
                            child: FilledButton(
                              onPressed: () => _respond(context, ref, c, true),
                              child: const Text('Accept'),
                            ),
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              ),
            ),
          ),
      ],
    );
  }
}

class _Empty extends StatelessWidget {
  const _Empty({required this.title, required this.body, this.action, this.onAction});

  final String title;
  final String body;
  final String? action;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return LayoutBuilder(
      builder: (context, constraints) => SingleChildScrollView(
        physics: const AlwaysScrollableScrollPhysics(),
        child: ConstrainedBox(
          constraints: BoxConstraints(minHeight: constraints.maxHeight),
          child: Padding(
            padding: const EdgeInsets.all(32),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                const RadiusMark(size: 56),
                const SizedBox(height: 20),
                Text(title, style: text.titleLarge, textAlign: TextAlign.center),
                const SizedBox(height: 8),
                Text(
                  body,
                  style: text.bodyMedium?.copyWith(color: Palette.muted),
                  textAlign: TextAlign.center,
                ),
                if (action != null) ...[
                  const SizedBox(height: 20),
                  OutlinedButton(onPressed: onAction, child: Text(action!)),
                ],
              ],
            ),
          ),
        ),
      ),
    );
  }
}
