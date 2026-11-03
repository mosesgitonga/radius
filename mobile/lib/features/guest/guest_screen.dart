import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/api_exception.dart';
import '../../core/location.dart';
import '../../core/providers.dart';
import '../../core/router.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../feed/feed_models.dart';
import '../feed/widgets/post_card.dart';
import 'guest_repository.dart';
import 'join_sheet.dart';

/// Home for someone who hasn't signed up: the feed around where the phone is,
/// with no names (decision M.9). Taking part, or any other tab, asks them to join.
class GuestScreen extends ConsumerStatefulWidget {
  const GuestScreen({super.key});

  @override
  ConsumerState<GuestScreen> createState() => _GuestScreenState();
}

class _GuestScreenState extends ConsumerState<GuestScreen> {
  final _scroll = ScrollController();
  Reading? _reading;
  GuestFeed? _feed;
  List<Post> _posts = const [];
  String? _problem;
  bool _loadingMore = false;

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) _loadMore();
    });
    _start();
  }

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  Future<void> _start() async {
    setState(() => _problem = null);
    try {
      final reading = _reading ?? await ref.read(locationReaderProvider).readHere();
      final feed = await ref.read(guestRepositoryProvider).feed(reading);
      if (!mounted) return;
      setState(() {
        _reading = reading;
        _feed = feed;
        _posts = feed.posts;
      });
    } on LocationProblem catch (problem) {
      if (mounted) setState(() => _problem = problem.message);
    } on ApiException catch (error) {
      if (mounted) setState(() => _problem = error.message);
    } catch (_) {
      // Anything else (no GPS hardware, a plugin missing): never hang on "Finding…".
      if (mounted) setState(() => _problem = "Couldn't read your location. Try again, or sign up.");
    }
  }

  Future<void> _loadMore() async {
    final reading = _reading;
    final cursor = _feed?.nextCursor;
    if (reading == null || cursor == null || _loadingMore) return;
    setState(() => _loadingMore = true);
    try {
      final next = await ref.read(guestRepositoryProvider).feed(reading, cursor: cursor);
      if (mounted) {
        setState(() {
          _feed = next;
          _posts = [..._posts, ...next.posts];
        });
      }
    } on ApiException {
      // A failed page leaves what is shown; scrolling again retries.
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  void _join(String toDo) => showJoinSheet(context, toDo: toDo);

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final feed = _feed;
    return Scaffold(
      floatingActionButton: feed == null
          ? null
          : FloatingActionButton(
              onPressed: () => _join('post'),
              tooltip: 'New post',
              backgroundColor: Palette.primary,
              foregroundColor: Palette.white,
              child: const Icon(Icons.add_rounded, size: 28),
            ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: 0,
        onDestinationSelected: (index) {
          if (index == 0) return;
          _join(const ['', 'buy and sell', 'chat with neighbours', 'have a profile'][index]);
        },
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.home_outlined),
            selectedIcon: Icon(Icons.home_rounded),
            label: 'Home',
          ),
          NavigationDestination(
            icon: Icon(Icons.shopping_bag_outlined),
            selectedIcon: Icon(Icons.shopping_bag_rounded),
            label: 'Market',
          ),
          NavigationDestination(
            icon: Icon(Icons.chat_bubble_outline_rounded),
            selectedIcon: Icon(Icons.chat_bubble_rounded),
            label: 'Chats',
          ),
          NavigationDestination(
            icon: Icon(Icons.person_outline_rounded),
            selectedIcon: Icon(Icons.person_rounded),
            label: 'Me',
          ),
        ],
      ),
      body: SafeArea(
        child: _problem != null
            ? _Problem(message: _problem!, onRetry: _start)
            : feed == null
                ? const _Finding()
                : RefreshIndicator(
                    color: Palette.primary,
                    onRefresh: () async {
                      _reading = null;
                      await _start();
                    },
                    child: CustomScrollView(
                      controller: _scroll,
                      physics: const AlwaysScrollableScrollPhysics(),
                      slivers: [
                        SliverToBoxAdapter(
                          child: Padding(
                            padding: const EdgeInsets.fromLTRB(16, 18, 12, 12),
                            child: Row(
                              children: [
                                Expanded(
                                  child: Column(
                                    crossAxisAlignment: CrossAxisAlignment.start,
                                    children: [
                                      Text(feed.area.label, style: text.headlineSmall),
                                      const SizedBox(height: 2),
                                      Text('Posts from around you', style: text.bodySmall),
                                    ],
                                  ),
                                ),
                                FilledButton(
                                  onPressed: () => context.push(Routes.signIn),
                                  style: FilledButton.styleFrom(
                                    backgroundColor: Palette.primary,
                                    minimumSize: const Size(0, 40),
                                  ),
                                  child: const Text('Sign up'),
                                ),
                              ],
                            ),
                          ),
                        ),
                        if (_posts.isEmpty)
                          SliverFillRemaining(
                            hasScrollBody: false,
                            child: Padding(
                              padding: const EdgeInsets.all(32),
                              child: Column(
                                mainAxisAlignment: MainAxisAlignment.center,
                                children: [
                                  const RadiusMark(size: 56),
                                  const SizedBox(height: 20),
                                  Text('Nothing here yet', style: text.titleLarge),
                                  const SizedBox(height: 8),
                                  Text(
                                    'Be the first in ${feed.area.label}: join and post a question, a '
                                    'recommendation, or something lost or found.',
                                    style: text.bodyMedium?.copyWith(color: Palette.muted),
                                    textAlign: TextAlign.center,
                                  ),
                                  const SizedBox(height: 20),
                                  FilledButton(
                                    onPressed: () => context.push(Routes.signIn),
                                    child: const Text('Sign up'),
                                  ),
                                ],
                              ),
                            ),
                          )
                        else ...[
                          SliverList.separated(
                            itemCount: _posts.length,
                            separatorBuilder: (_, _) => const SizedBox(height: 10),
                            itemBuilder: (context, index) => Padding(
                              padding: const EdgeInsets.symmetric(horizontal: 12),
                              child: PostCard(
                                post: _posts[index],
                                onHelpful: () => _join('mark posts helpful'),
                                onOpen: () => _join('read the comments'),
                                onMore: () => _join('message or report'),
                                onAuthor: () => _join('see who posted'),
                              ),
                            ),
                          ),
                          SliverToBoxAdapter(
                            child: Padding(
                              padding: const EdgeInsets.fromLTRB(16, 16, 16, 96),
                              child: Center(
                                child: _loadingMore
                                    ? const CircularProgressIndicator(color: Palette.primary)
                                    : Text(
                                        feed.nextCursor == null ? 'Join to see more and take part' : '',
                                        style: text.bodySmall,
                                      ),
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
}

class _Finding extends StatelessWidget {
  const _Finding();

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Center(
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          const RadiusMark(size: 56),
          const SizedBox(height: 20),
          Text('Finding your neighbourhood…', style: text.titleMedium),
          const SizedBox(height: 16),
          const CircularProgressIndicator(color: Palette.primary),
        ],
      ),
    );
  }
}

class _Problem extends StatelessWidget {
  const _Problem({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

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
          Text('See what is happening near you', style: text.titleLarge, textAlign: TextAlign.center),
          const SizedBox(height: 8),
          Text(message, style: text.bodyMedium?.copyWith(color: Palette.muted), textAlign: TextAlign.center),
          const SizedBox(height: 20),
          FilledButton(onPressed: onRetry, child: const Text('Try again')),
          const SizedBox(height: 4),
          TextButton(
            onPressed: () => context.push(Routes.signIn),
            child: const Text('Sign up instead'),
          ),
        ],
      ),
    );
  }
}
