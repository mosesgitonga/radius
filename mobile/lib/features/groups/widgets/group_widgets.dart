import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../feed/feed_models.dart';
import '../group_models.dart';

/// A group's picture: its cover, or its first letter on a tint while there is none.
class GroupBadge extends StatelessWidget {
  const GroupBadge({super.key, required this.group, this.size = 48});

  final Group group;
  final double size;

  @override
  Widget build(BuildContext context) {
    final letter = Container(
      width: size,
      height: size,
      alignment: Alignment.center,
      decoration: BoxDecoration(color: Palette.glowTint, borderRadius: BorderRadius.circular(size / 4)),
      child: Text(
        group.name.trim().isEmpty ? '?' : group.name.trim()[0].toUpperCase(),
        style: Theme.of(context).textTheme.titleLarge?.copyWith(color: Palette.primaryDark, fontSize: size * 0.42),
      ),
    );
    final url = sizeFor(group.cover, (size * MediaQuery.devicePixelRatioOf(context)).round());
    if (url == null) return letter;
    return ClipRRect(
      borderRadius: BorderRadius.circular(size / 4),
      child: CachedNetworkImage(
        imageUrl: url,
        width: size,
        height: size,
        fit: BoxFit.cover,
        placeholder: (_, _) => letter,
        errorWidget: (_, _, _) => letter,
      ),
    );
  }
}

/// One group in a list: picture, name, where and how big, and where you stand.
class GroupTile extends StatelessWidget {
  const GroupTile({super.key, required this.group, required this.onTap, this.trailing});

  final Group group;
  final VoidCallback onTap;
  final Widget? trailing;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final standing = group.isMember
        ? null
        : group.hasAsked
            ? 'Requested'
            : group.isInvited
                ? 'Invited'
                : null;
    return Material(
      color: Palette.surface,
      borderRadius: BorderRadius.circular(16),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Row(
            children: [
              GroupBadge(group: group),
              const SizedBox(width: 12),
              Expanded(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Row(
                      children: [
                        if (!group.isPublic) ...[
                          Icon(Icons.lock_outline_rounded, size: 15, color: Palette.muted),
                          const SizedBox(width: 4),
                        ],
                        Flexible(
                          child: Text(group.name, style: text.titleSmall, overflow: TextOverflow.ellipsis),
                        ),
                      ],
                    ),
                    const SizedBox(height: 2),
                    Text(
                      [group.categoryLabel, group.meta].join(' · '),
                      style: text.bodySmall,
                      overflow: TextOverflow.ellipsis,
                    ),
                  ],
                ),
              ),
              if (trailing != null)
                trailing!
              else if (standing != null)
                Text(standing, style: text.labelMedium?.copyWith(color: Palette.primary)),
            ],
          ),
        ),
      ),
    );
  }
}

class GroupCategoryChip extends StatelessWidget {
  const GroupCategoryChip({super.key, required this.label, required this.selected, required this.onTap});

  final String label;
  final bool selected;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        label: Text(label),
        selected: selected,
        onSelected: onTap == null ? null : (_) => onTap!(),
        showCheckmark: false,
        selectedColor: Palette.ink,
        labelStyle: TextStyle(color: selected ? Palette.onPrimary : Palette.ink, fontSize: 13.5),
        backgroundColor: Palette.surface,
        side: BorderSide(color: selected ? Palette.ink : Palette.lineStrong),
        shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(17)),
      ),
    );
  }
}

/// "Join with a link": paste a group's invite link, see which group it is, and join.
Future<void> showJoinWithLink(BuildContext context) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      backgroundColor: Palette.cream,
      builder: (_) => const _JoinWithLinkSheet(),
    );

class _JoinWithLinkSheet extends ConsumerStatefulWidget {
  const _JoinWithLinkSheet();

  @override
  ConsumerState<_JoinWithLinkSheet> createState() => _JoinWithLinkSheetState();
}

class _JoinWithLinkSheetState extends ConsumerState<_JoinWithLinkSheet> {
  final _link = TextEditingController();
  Group? _found;
  String? _token;
  String? _error;
  bool _busy = false;

  @override
  void dispose() {
    _link.dispose();
    super.dispose();
  }

  Future<void> _look() async {
    final token = inviteTokenFrom(_link.text);
    if (token == null) {
      setState(() => _error = "That doesn't look like a group link.");
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final group = await ref.read(groupRepositoryProvider).previewLink(token);
      setState(() {
        _found = group;
        _token = token;
      });
    } on ApiException catch (error) {
      setState(() => _error = error.statusCode == 404 ? 'This link has expired or no longer works.' : error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _join() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final group = await ref.read(groupRepositoryProvider).joinWithLink(_token!);
      if (!mounted) return;
      Navigator.of(context).pop();
      GoRouter.of(context).go(Routes.group(group.id));
    } on ApiException catch (error) {
      setState(() {
        _busy = false;
        _error = error.statusCode == 404 ? 'This link has expired or no longer works.' : error.message;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final found = _found;
    return Padding(
      padding: EdgeInsets.fromLTRB(20, 20, 20, 20 + MediaQuery.viewInsetsOf(context).bottom),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Text('Join with a link', style: text.titleLarge),
          const SizedBox(height: 6),
          Text('Paste the invite link someone sent you.', style: text.bodyMedium?.copyWith(color: Palette.muted)),
          const SizedBox(height: 14),
          if (found == null) ...[
            TextField(
              controller: _link,
              autofocus: true,
              enabled: !_busy,
              decoration: const InputDecoration(hintText: 'https://…/g/…'),
              onSubmitted: (_) => _look(),
            ),
            FormError(_error),
            const SizedBox(height: 14),
            BusyButton(label: 'Find the group', busy: _busy, onPressed: _busy ? null : _look),
          ] else ...[
            GroupTile(group: found, onTap: () {}),
            if (found.description.isNotEmpty) ...[
              const SizedBox(height: 10),
              Text(found.description, maxLines: 4, overflow: TextOverflow.ellipsis, style: text.bodyMedium),
            ],
            FormError(_error),
            const SizedBox(height: 14),
            if (found.isMember)
              FilledButton(
                onPressed: () {
                  Navigator.of(context).pop();
                  GoRouter.of(context).go(Routes.group(found.id));
                },
                child: const Text("You're in: open the group"),
              )
            else
              BusyButton(label: 'Join group', busy: _busy, onPressed: _busy ? null : _join),
          ],
        ],
      ),
    );
  }
}
