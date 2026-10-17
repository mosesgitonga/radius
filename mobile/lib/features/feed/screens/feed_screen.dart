import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../feed_controller.dart';
import '../widgets/post_card.dart';
import '../widgets/report_sheet.dart';

class FeedScreen extends ConsumerStatefulWidget {
  const FeedScreen({super.key});

  @override
  ConsumerState<FeedScreen> createState() => _FeedScreenState();
}

class _FeedScreenState extends ConsumerState<FeedScreen> {
  final _scroll = ScrollController();

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) {
        ref.read(feedControllerProvider.notifier).loadMore();
      }
    });
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _toggleHelpful(String postId) async {
    try {
      await ref.read(feedControllerProvider.notifier).toggleHelpful(postId);
    } on ApiException catch (error) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
      }
    }
  }

  void _postMenu(String postId, bool mine) {
    showModalBottomSheet<void>(
      context: context,
      backgroundColor: Palette.cream,
      builder: (sheet) => SafeArea(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            if (!mine)
              ListTile(
                leading: const Icon(Icons.flag_outlined),
                title: const Text('Report post'),
                onTap: () {
                  Navigator.of(sheet).pop();
                  showReportSheet(
                    context,
                    what: 'post',
                    send: (reason, details) => ref.read(feedRepositoryProvider).report(
                          targetType: 'post',
                          targetId: postId,
                          reason: reason,
                          details: details,
                        ),
                  );
                },
              ),
            ListTile(
              leading: const Icon(Icons.close_rounded),
              title: const Text('Cancel'),
              onTap: () => Navigator.of(sheet).pop(),
            ),
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final feed = ref.watch(feedControllerProvider);
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    final area = me?.areaLabel?.label ?? 'Your area';
    final radius = feed.radiusKm;

    return Scaffold(
      floatingActionButton: FloatingActionButton(
        onPressed: () => context.push(Routes.compose),
        tooltip: 'New post',
        backgroundColor: Palette.primary,
        foregroundColor: Palette.white,
        child: const Icon(Icons.add_rounded, size: 28),
      ),
      body: SafeArea(
        child: RefreshIndicator(
          color: Palette.primary,
          onRefresh: () => ref.read(feedControllerProvider.notifier).refresh(),
          child: CustomScrollView(
            controller: _scroll,
            physics: const AlwaysScrollableScrollPhysics(),
            slivers: [
              SliverToBoxAdapter(
                child: Padding(
                  padding: const EdgeInsets.fromLTRB(16, 18, 16, 12),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(area, style: text.headlineSmall),
                      const SizedBox(height: 2),
                      Text(
                        radius == null ? 'Around you' : 'Within ${_km(radius)} km of home',
                        style: text.bodySmall,
                      ),
                    ],
                  ),
                ),
              ),
              if (feed.loading && feed.posts.isEmpty)
                const SliverFillRemaining(
                  hasScrollBody: false,
                  child: Center(child: CircularProgressIndicator(color: Palette.primary)),
                )
              else if (feed.error != null && feed.posts.isEmpty)
                SliverFillRemaining(
                  hasScrollBody: false,
                  child: _Message(
                    icon: Icons.wifi_off_rounded,
                    title: "Couldn't load the feed",
                    body: feed.error!.message,
                    action: 'Try again',
                    onAction: () => ref.read(feedControllerProvider.notifier).refresh(),
                  ),
                )
              else if (feed.posts.isEmpty)
                SliverFillRemaining(
                  hasScrollBody: false,
                  child: _Message(
                    icon: Icons.forum_outlined,
                    title: 'Nothing here yet',
                    body: 'Be the first to post in $area: a question, a recommendation, '
                        'something lost or found.',
                    action: 'Write a post',
                    onAction: () => context.push(Routes.compose),
                  ),
                )
              else ...[
                SliverList.separated(
                  itemCount: feed.posts.length,
                  separatorBuilder: (_, _) => const SizedBox(height: 10),
                  itemBuilder: (context, index) {
                    final post = feed.posts[index];
                    final mine = post.author.id == me?.id;
                    return Padding(
                      padding: const EdgeInsets.symmetric(horizontal: 12),
                      child: PostCard(
                        post: post,
                        mine: mine,
                        onHelpful: () => _toggleHelpful(post.id),
                        onOpen: () => context.go(Routes.post(post.id)),
                        onMore: () => _postMenu(post.id, mine),
                      ),
                    );
                  },
                ),
                SliverToBoxAdapter(
                  child: Padding(
                    padding: const EdgeInsets.fromLTRB(16, 16, 16, 96),
                    child: Center(
                      child: feed.loadingMore
                          ? const CircularProgressIndicator(color: Palette.primary)
                          : Text(feed.hasMore ? '' : "You're all caught up", style: text.bodySmall),
                    ),
                  ),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  static String _km(double value) =>
      value == value.roundToDouble() ? value.toStringAsFixed(0) : value.toStringAsFixed(1);
}

class _Message extends StatelessWidget {
  const _Message({
    required this.icon,
    required this.title,
    required this.body,
    required this.action,
    required this.onAction,
  });

  final IconData icon;
  final String title;
  final String body;
  final String action;
  final VoidCallback onAction;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: const EdgeInsets.all(32),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const RadiusMark(size: 56),
          const SizedBox(height: 20),
          Text(title, style: text.titleLarge, textAlign: TextAlign.center),
          const SizedBox(height: 8),
          Text(body, style: text.bodyMedium?.copyWith(color: Palette.muted), textAlign: TextAlign.center),
          const SizedBox(height: 20),
          OutlinedButton(onPressed: onAction, child: Text(action)),
        ],
      ),
    );
  }
}
