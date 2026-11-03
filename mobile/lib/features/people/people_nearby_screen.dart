import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../auth/auth_controller.dart';
import '../auth/auth_models.dart';
import '../feed/widgets/post_card.dart';
import '../profile/profile_models.dart';
import '../profile/profile_screen.dart';

/// Neighbours who chose to be listed, for proven neighbours only (decision M.8).
class PeopleNearbyScreen extends ConsumerStatefulWidget {
  const PeopleNearbyScreen({super.key});

  @override
  ConsumerState<PeopleNearbyScreen> createState() => _PeopleNearbyScreenState();
}

class _PeopleNearbyScreenState extends ConsumerState<PeopleNearbyScreen> {
  List<Profile>? _people;
  String? _cursor;
  String? _error;
  bool _loadingMore = false;
  bool _turningOn = false;

  @override
  void initState() {
    super.initState();
    final auth = ref.read(authControllerProvider);
    if (auth is AuthSignedIn && auth.me.canContactStrangers) _load();
  }

  Future<void> _load({bool more = false}) async {
    if (more) setState(() => _loadingMore = true);
    try {
      final (people, cursor) =
          await ref.read(accountRepositoryProvider).peopleNearby(cursor: more ? _cursor : null);
      if (!mounted) return;
      setState(() {
        _people = more ? [...?_people, ...people] : people;
        _cursor = cursor;
        _error = null;
      });
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _loadingMore = false);
    }
  }

  Future<void> _showMe() async {
    setState(() => _turningOn = true);
    try {
      final me = await ref.read(accountRepositoryProvider).setShowInNearby(true);
      ref.read(authControllerProvider.notifier).replaceMe(me);
    } on ApiException catch (error) {
      if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    } finally {
      if (mounted) setState(() => _turningOn = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    return Scaffold(
      appBar: AppBar(title: const Text('People nearby')),
      body: me == null
          ? const SizedBox.shrink()
          : !me.canContactStrangers
              ? _NotYet(me: me)
              : RefreshIndicator(
                  color: Palette.primary,
                  onRefresh: _load,
                  child: ListView(
                    physics: const AlwaysScrollableScrollPhysics(),
                    padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
                    children: [
                      if (!me.showInNearby) ...[
                        _ShowMeCard(busy: _turningOn, onShowMe: _showMe),
                        const SizedBox(height: 16),
                      ],
                      ..._list(context),
                    ],
                  ),
                ),
    );
  }

  List<Widget> _list(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final people = _people;
    if (_error != null && people == null) {
      return [
        const SizedBox(height: 40),
        Center(child: Text(_error!, textAlign: TextAlign.center)),
        const SizedBox(height: 12),
        Center(child: TextButton(onPressed: _load, child: const Text('Try again'))),
      ];
    }
    if (people == null) {
      return const [
        SizedBox(height: 60),
        Center(child: CircularProgressIndicator(color: Palette.primary)),
      ];
    }
    if (people.isEmpty) {
      return [
        const SizedBox(height: 40),
        const Icon(Icons.people_outline_rounded, size: 40, color: Palette.muted),
        const SizedBox(height: 12),
        Text('Nobody nearby is listed yet', style: text.titleMedium, textAlign: TextAlign.center),
        const SizedBox(height: 4),
        Text(
          'Neighbours appear here once they choose to be listed.',
          style: text.bodySmall,
          textAlign: TextAlign.center,
        ),
      ];
    }
    return [
      for (final person in people) _PersonTile(person: person),
      if (_cursor != null)
        Center(
          child: TextButton(
            onPressed: _loadingMore ? null : () => _load(more: true),
            child: Text(_loadingMore ? 'Loading…' : 'Show more'),
          ),
        ),
    ];
  }
}

class _PersonTile extends ConsumerWidget {
  const _PersonTile({required this.person});

  final Profile person;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = Theme.of(context).textTheme;
    final bio = person.bio?.split('\n').first;
    return ListTile(
      contentPadding: const EdgeInsets.symmetric(vertical: 4),
      leading: InitialsAvatar(author: person.asAuthor, radius: 22),
      title: Row(
        children: [
          Flexible(child: Text(person.displayName, overflow: TextOverflow.ellipsis)),
          if (person.isVerifiedNeighbour) ...[const SizedBox(width: 6), const NeighbourBadge()],
        ],
      ),
      subtitle: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(person.where, style: text.bodySmall),
          if (bio != null && bio.isNotEmpty)
            Text(bio, maxLines: 1, overflow: TextOverflow.ellipsis, style: text.bodySmall),
        ],
      ),
      onTap: () => openProfile(context, ref, person.id),
    );
  }
}

class _ShowMeCard extends StatelessWidget {
  const _ShowMeCard({required this.busy, required this.onShowMe});

  final bool busy;
  final VoidCallback onShowMe;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: Palette.white,
        border: Border.all(color: Palette.line),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text("You're not listed here", style: text.titleSmall),
          const SizedBox(height: 4),
          Text(
            'Neighbours whose feed reaches your home would see your name, photo, bio and area, '
            'never where you live. You can turn this off in Privacy at any time.',
            style: text.bodySmall?.copyWith(color: Palette.ink),
          ),
          const SizedBox(height: 10),
          BusyButton(label: 'Show me here', busy: busy, onPressed: onShowMe),
        ],
      ),
    );
  }
}

class _NotYet extends StatelessWidget {
  const _NotYet({required this.me});

  final Me me;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final needed = me.contactUnlockDays;
    final soFar = min(me.presenceDaysThisWeek, needed);
    return Padding(
      padding: const EdgeInsets.all(24),
      child: Column(
        mainAxisAlignment: MainAxisAlignment.center,
        children: [
          const Icon(Icons.home_outlined, size: 40, color: Palette.muted),
          const SizedBox(height: 12),
          Text('Settle in first', style: text.titleMedium),
          const SizedBox(height: 6),
          Text(
            "People nearby opens once you've been at home in your area on $needed of the last "
            '${me.contactUnlockWindowDays} days. So far: $soFar of $needed.',
            style: text.bodyMedium,
            textAlign: TextAlign.center,
          ),
        ],
      ),
    );
  }
}
