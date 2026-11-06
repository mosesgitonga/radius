import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/router.dart';
import '../../core/theme.dart';
import '../auth/auth_controller.dart';
import '../feed/feed_models.dart';
import '../feed/widgets/post_card.dart';
import 'stories_controller.dart';
import 'story_models.dart';
import 'story_viewer_screen.dart';

/// The row of stories on Home: mine first, then neighbours with something new,
/// then the rest. A coloured ring means not yet watched.
class StoriesStrip extends ConsumerWidget {
  const StoriesStrip({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    ref.watch(storiesControllerProvider);
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    if (me == null) return const SizedBox.shrink();
    final groups = ref.read(storiesControllerProvider.notifier).groups;
    final mine = groups.where((g) => g.author.id == me.id).firstOrNull;
    final others = [
      for (final g in groups)
        if (g.author.id != me.id) g,
    ];

    void open(StoryGroup group) {
      final all = [?mine, ...others];
      Navigator.of(context, rootNavigator: true).push(
        MaterialPageRoute<void>(
          fullscreenDialog: true,
          builder: (_) => StoryViewerScreen(groups: all, startGroup: all.indexOf(group)),
        ),
      );
    }

    final myAuthor = Author(id: me.id, displayName: me.displayName, isVerifiedNeighbour: false, avatar: me.avatar);
    return SizedBox(
      height: 98,
      child: ListView(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 12),
        children: [
          _Bubble(
            author: myAuthor,
            label: 'Your story',
            ring: mine == null ? null : (mine.allSeen ? Palette.line : Palette.primary),
            badge: true,
            onTap: () => mine == null ? context.push(Routes.newStory) : open(mine),
            onBadge: () => context.push(Routes.newStory),
          ),
          for (final group in others)
            _Bubble(
              author: group.author,
              label: group.author.displayName.split(' ').first,
              ring: group.allSeen ? Palette.line : Palette.primary,
              onTap: () => open(group),
            ),
        ],
      ),
    );
  }
}

class _Bubble extends StatelessWidget {
  const _Bubble({
    required this.author,
    required this.label,
    required this.onTap,
    this.ring,
    this.badge = false,
    this.onBadge,
  });

  final Author author;
  final String label;
  final Color? ring;
  final bool badge;
  final VoidCallback onTap;
  final VoidCallback? onBadge;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.symmetric(horizontal: 4),
      child: SizedBox(
        width: 70,
        child: GestureDetector(
          behavior: HitTestBehavior.opaque,
          onTap: onTap,
          child: Column(
            children: [
              Stack(
                clipBehavior: Clip.none,
                children: [
                  Container(
                    padding: const EdgeInsets.all(3),
                    decoration: BoxDecoration(
                      shape: BoxShape.circle,
                      border: Border.all(color: ring ?? Colors.transparent, width: 2.5),
                    ),
                    child: InitialsAvatar(author: author, radius: 27),
                  ),
                  if (badge)
                    Positioned(
                      right: -2,
                      bottom: -2,
                      child: GestureDetector(
                        onTap: onBadge,
                        child: Container(
                          decoration: BoxDecoration(
                            color: Palette.primary,
                            shape: BoxShape.circle,
                            border: Border.all(color: Palette.cream, width: 2),
                          ),
                          padding: const EdgeInsets.all(2),
                          child: const Icon(Icons.add_rounded, size: 18, color: Palette.onPrimary),
                        ),
                      ),
                    ),
                ],
              ),
              const SizedBox(height: 4),
              Text(label, maxLines: 1, overflow: TextOverflow.ellipsis, style: text.bodySmall),
            ],
          ),
        ),
      ),
    );
  }
}
