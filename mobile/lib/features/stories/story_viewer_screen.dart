import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/theme.dart';
import '../auth/auth_controller.dart';
import '../chats/start_chat.dart';
import '../feed/feed_models.dart';
import '../feed/widgets/post_card.dart';
import '../feed/widgets/report_sheet.dart';
import 'stories_controller.dart';
import 'story_models.dart';
import 'story_repository.dart';

/// Stories, full screen: five seconds each, tap to move, hold to pause, and on
/// to the next person at the end of one person's stories.
class StoryViewerScreen extends ConsumerStatefulWidget {
  const StoryViewerScreen({super.key, required this.groups, required this.startGroup});

  final List<StoryGroup> groups;
  final int startGroup;

  static const storyDuration = Duration(seconds: 5);

  @override
  ConsumerState<StoryViewerScreen> createState() => _StoryViewerScreenState();
}

class _StoryViewerScreenState extends ConsumerState<StoryViewerScreen> with SingleTickerProviderStateMixin {
  late final AnimationController _clock;
  late int _group;
  late int _index;

  StoryGroup get _current => widget.groups[_group];
  Story get _story => _current.stories[_index];

  bool get _mine {
    final auth = ref.read(authControllerProvider);
    return auth is AuthSignedIn && auth.me.id == _current.author.id;
  }

  @override
  void initState() {
    super.initState();
    _group = widget.startGroup.clamp(0, widget.groups.length - 1);
    _index = _current.firstUnseen;
    _clock = AnimationController(vsync: this, duration: StoryViewerScreen.storyDuration)
      ..addStatusListener((status) {
        if (status == AnimationStatus.completed) _next();
      });
    // After the first frame: marking a story seen changes shared state, which
    // may not happen while the screen is being built.
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (mounted) _show();
    });
  }

  @override
  void dispose() {
    _clock.dispose();
    super.dispose();
  }

  void _show() {
    if (!_mine) ref.read(storiesControllerProvider.notifier).markSeen(_story);
    _clock.forward(from: 0);
  }

  void _next() {
    if (_index < _current.stories.length - 1) {
      setState(() => _index++);
    } else if (_group < widget.groups.length - 1) {
      setState(() {
        _group++;
        _index = _current.firstUnseen;
      });
    } else {
      Navigator.of(context).maybePop();
      return;
    }
    _show();
  }

  void _previous() {
    if (_index > 0) {
      setState(() => _index--);
    } else if (_group > 0) {
      setState(() {
        _group--;
        _index = 0;
      });
    }
    _show();
  }

  Future<void> _paused(Future<void> Function() action) async {
    _clock.stop();
    await action();
    if (mounted) _clock.forward();
  }

  Future<void> _showViewers() => _paused(() async {
    List<StoryViewer>? viewers;
    String? error;
    try {
      viewers = await ref.read(storyRepositoryProvider).viewers(_story.id);
    } on ApiException catch (problem) {
      error = problem.message;
    }
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: viewers == null
            ? Padding(padding: const EdgeInsets.all(24), child: Text(error ?? ''))
            : viewers.isEmpty
            ? const Padding(padding: EdgeInsets.all(24), child: Text('Nobody has watched it yet.'))
            : ListView(
                shrinkWrap: true,
                children: [
                  for (final viewer in viewers)
                    ListTile(
                      leading: InitialsAvatar(author: viewer.user),
                      title: Text(viewer.user.displayName),
                    ),
                ],
              ),
      ),
    );
  });

  /// Your own story, from the button beside the view count: asks first.
  Future<void> _confirmDelete() => _paused(() async {
    final story = _story;
    final sure = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: const Text('Delete this story?'),
        content: const Text('It disappears for everyone now, before its 24 hours are up.'),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Keep')),
          TextButton(onPressed: () => Navigator.of(dialog).pop(true), child: const Text('Delete')),
        ],
      ),
    );
    if (sure != true || !mounted) return;
    try {
      await ref.read(storiesControllerProvider.notifier).delete(story.id);
      if (mounted) Navigator.of(context).maybePop();
    } on ApiException catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    }
  });

  Future<void> _menu() => _paused(() async {
    final story = _story;
    final mine = _mine;
    final choice = await showModalBottomSheet<String>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (mine)
              ListTile(
                leading: const Icon(Icons.delete_outline_rounded),
                title: const Text('Delete story'),
                onTap: () => Navigator.of(sheet).pop('delete'),
              )
            else ...[
              ListTile(
                leading: const Icon(Icons.chat_bubble_outline_rounded),
                title: Text('Message ${story.author.displayName}'),
                onTap: () => Navigator.of(sheet).pop('message'),
              ),
              ListTile(
                leading: const Icon(Icons.flag_outlined),
                title: const Text('Report story'),
                onTap: () => Navigator.of(sheet).pop('report'),
              ),
            ],
            ListTile(
              leading: const Icon(Icons.close_rounded),
              title: const Text('Cancel'),
              onTap: () => Navigator.of(sheet).pop(),
            ),
          ],
        ),
      ),
    );
    if (!mounted || choice == null) return;
    switch (choice) {
      case 'delete':
        try {
          await ref.read(storiesControllerProvider.notifier).delete(story.id);
          if (mounted) Navigator.of(context).maybePop();
        } on ApiException catch (error) {
          if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
        }
      case 'message':
        Navigator.of(context).maybePop();
        await openDirectChat(context, ref, story.author.id);
      case 'report':
        await showReportSheet(
          context,
          what: 'story',
          send: (reason, details) => ref
              .read(feedRepositoryProvider)
              .report(targetType: 'story', targetId: story.id, reason: reason, details: details),
        );
    }
  });

  @override
  Widget build(BuildContext context) {
    final story = _story;
    final text = Theme.of(context).textTheme;
    final width = MediaQuery.sizeOf(context).width;
    final pixels = (width * MediaQuery.devicePixelRatioOf(context)).round();
    final where = [story.areaLabel.label, ?distanceLabel(story.distanceBucket), _ago(story.createdAt)].join(' · ');

    return Scaffold(
      backgroundColor: Colors.black,
      body: GestureDetector(
        onTapUp: (details) => details.localPosition.dx < width / 3 ? _previous() : _next(),
        onLongPressStart: (_) => _clock.stop(),
        onLongPressEnd: (_) => _clock.forward(),
        onVerticalDragEnd: (details) {
          if ((details.primaryVelocity ?? 0) > 300) Navigator.of(context).maybePop();
        },
        child: Stack(
          fit: StackFit.expand,
          children: [
            if (story.isImage)
              CachedNetworkImage(
                imageUrl: sizeFor(story.image, pixels) ?? '',
                fit: BoxFit.contain,
                placeholder: (_, _) => const Center(child: CircularProgressIndicator(color: Palette.onPrimary)),
                errorWidget: (_, _, _) =>
                    const Center(child: Icon(Icons.broken_image_outlined, color: Palette.onPrimary, size: 40)),
              )
            else
              Container(
                color: Color(storyColour(story.id)),
                alignment: Alignment.center,
                padding: const EdgeInsets.all(32),
                child: Text(
                  story.text ?? '',
                  textAlign: TextAlign.center,
                  style: text.headlineSmall?.copyWith(color: Palette.onPrimary, height: 1.3),
                ),
              ),
            SafeArea(
              child: Column(
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(8, 8, 8, 0),
                    child: Row(
                      children: [
                        for (var i = 0; i < _current.stories.length; i++)
                          Expanded(
                            child: Padding(
                              padding: const EdgeInsets.symmetric(horizontal: 2),
                              child: AnimatedBuilder(
                                animation: _clock,
                                builder: (_, _) => LinearProgressIndicator(
                                  value: i < _index ? 1 : (i == _index ? _clock.value : 0),
                                  minHeight: 3,
                                  backgroundColor: Colors.white24,
                                  color: Palette.onPrimary,
                                ),
                              ),
                            ),
                          ),
                      ],
                    ),
                  ),
                  ListTile(
                    leading: InitialsAvatar(author: story.author),
                    title: Text(story.author.displayName, style: text.titleSmall?.copyWith(color: Palette.onPrimary)),
                    subtitle: Text(where, style: text.bodySmall?.copyWith(color: Colors.white70)),
                    trailing: Row(
                      mainAxisSize: MainAxisSize.min,
                      children: [
                        IconButton(
                          tooltip: 'More',
                          icon: const Icon(Icons.more_vert_rounded, color: Palette.onPrimary),
                          onPressed: _menu,
                        ),
                        IconButton(
                          tooltip: 'Close',
                          icon: const Icon(Icons.close_rounded, color: Palette.onPrimary),
                          onPressed: () => Navigator.of(context).maybePop(),
                        ),
                      ],
                    ),
                  ),
                  const Spacer(),
                  if (_mine)
                    Padding(
                      padding: const EdgeInsets.fromLTRB(8, 0, 8, 16),
                      child: Row(
                        mainAxisAlignment: MainAxisAlignment.spaceBetween,
                        children: [
                          TextButton.icon(
                            onPressed: _showViewers,
                            icon: const Icon(Icons.visibility_outlined, color: Palette.onPrimary),
                            label: Text(
                              '${story.viewCount ?? 0} ${story.viewCount == 1 ? 'view' : 'views'}',
                              style: text.titleSmall?.copyWith(color: Palette.onPrimary),
                            ),
                          ),
                          TextButton.icon(
                            onPressed: _confirmDelete,
                            icon: const Icon(Icons.delete_outline_rounded, color: Palette.onPrimary),
                            label: Text('Delete', style: text.titleSmall?.copyWith(color: Palette.onPrimary)),
                          ),
                        ],
                      ),
                    ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  static String _ago(DateTime when) {
    final minutes = DateTime.now().difference(when).inMinutes;
    if (minutes < 1) return 'just now';
    if (minutes < 60) return '${minutes}m';
    return '${minutes ~/ 60}h';
  }
}
