import 'dart:async';

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
import '../widgets/group_widgets.dart';

/// Groups: the ones you're in, invitations waiting for you, and public groups
/// nearby to find and join.
class GroupsScreen extends ConsumerStatefulWidget {
  const GroupsScreen({super.key});

  @override
  ConsumerState<GroupsScreen> createState() => _GroupsScreenState();
}

class _GroupsScreenState extends ConsumerState<GroupsScreen> {
  final _search = TextEditingController();
  Timer? _debounce;

  List<Group> _mine = const [];
  List<Group> _invitations = const [];
  List<Group> _nearby = const [];
  String? _nearbyCursor;
  double? _radiusKm;
  String? _category;
  bool _loading = true;
  bool _loadingNearby = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    super.dispose();
  }

  Future<void> _load() async {
    final repo = ref.read(groupRepositoryProvider);
    try {
      final results = await Future.wait([
        repo.mine(),
        repo.invitations(),
        repo.discover(query: _search.text, category: _category),
      ]);
      if (!mounted) return;
      final discovery = results[2] as GroupDiscovery;
      setState(() {
        _mine = (results[0] as ResultPage<Group>).items;
        _invitations = results[1] as List<Group>;
        _nearby = discovery.items;
        _nearbyCursor = discovery.nextCursor;
        _radiusKm = discovery.radiusKm;
        _loading = false;
        _error = null;
      });
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _loading = false;
          _error = error.message;
        });
      }
    }
  }

  Future<void> _discover({bool more = false}) async {
    if (more && (_nearbyCursor == null || _loadingNearby)) return;
    setState(() => _loadingNearby = true);
    try {
      final page = await ref
          .read(groupRepositoryProvider)
          .discover(query: _search.text, category: _category, cursor: more ? _nearbyCursor : null);
      if (!mounted) return;
      setState(() {
        _nearby = more ? [..._nearby, ...page.items] : page.items;
        _nearbyCursor = page.nextCursor;
        _radiusKm = page.radiusKm;
      });
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _loadingNearby = false);
    }
  }

  void _onSearch(String _) {
    setState(() {});
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), _discover);
  }

  Future<void> _answer(Group group, {required bool accept}) async {
    try {
      final updated = await ref.read(groupRepositoryProvider).answerInvitation(group.id, accept: accept);
      if (!mounted) return;
      setState(() {
        _invitations = [..._invitations.where((g) => g.id != group.id)];
        if (accept) _mine = [updated, ..._mine];
      });
      if (accept) context.go(Routes.group(group.id));
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _create() async {
    final group = await context.push<Group>(Routes.newGroup);
    if (group == null || !mounted) return;
    setState(() => _mine = [group, ..._mine]);
    context.go(Routes.group(group.id));
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final mineIds = {for (final group in _mine) group.id};
    final nearby = [for (final group in _nearby) if (!mineIds.contains(group.id)) group];
    final filtered = _search.text.trim().isNotEmpty || _category != null;

    Widget section(String title, [String? note]) => Padding(
          padding: const EdgeInsets.fromLTRB(4, 18, 4, 8),
          child: Row(
            crossAxisAlignment: CrossAxisAlignment.end,
            children: [
              Expanded(child: Text(title, style: text.titleMedium)),
              if (note != null) Text(note, style: text.bodySmall),
            ],
          ),
        );

    return Scaffold(
      appBar: AppBar(
        title: const Text('Groups'),
        actions: [
          IconButton(
            tooltip: 'Join with a link',
            onPressed: () => showJoinWithLink(context),
            icon: const Icon(Icons.link_rounded),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: FilledButton.icon(
              onPressed: _create,
              style: FilledButton.styleFrom(
                minimumSize: const Size(0, 40),
                padding: const EdgeInsets.symmetric(horizontal: 14),
              ),
              icon: const Icon(Icons.add_rounded, size: 18),
              label: const Text('New group'),
            ),
          ),
        ],
      ),
      body: RefreshIndicator(
        color: Palette.primary,
        onRefresh: _load,
        child: _loading
            ? ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                children: [
                  const SizedBox(height: 120),
                  Center(child: CircularProgressIndicator(color: Palette.primary)),
                ],
              )
            : NotificationListener<ScrollNotification>(
                onNotification: (note) {
                  if (note.metrics.pixels > note.metrics.maxScrollExtent - 400) _discover(more: true);
                  return false;
                },
                child: ListView(
                  physics: const AlwaysScrollableScrollPhysics(),
                  padding: const EdgeInsets.fromLTRB(12, 0, 12, 32),
                  children: [
                    if (_error != null) ...[
                      const SizedBox(height: 16),
                      FormError(_error),
                      TextButton(onPressed: _load, child: const Text('Try again')),
                    ],
                    if (_invitations.isNotEmpty) ...[
                      section('Invitations'),
                      for (final group in _invitations)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: GroupTile(
                            group: group,
                            onTap: () => context.go(Routes.group(group.id)),
                            trailing: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                IconButton(
                                  tooltip: 'Decline',
                                  onPressed: () => _answer(group, accept: false),
                                  icon: Icon(Icons.close_rounded, color: Palette.muted),
                                ),
                                IconButton.filled(
                                  tooltip: 'Accept',
                                  onPressed: () => _answer(group, accept: true),
                                  style: IconButton.styleFrom(backgroundColor: Palette.primary),
                                  icon: const Icon(Icons.check_rounded, color: Palette.onPrimary),
                                ),
                              ],
                            ),
                          ),
                        ),
                    ],
                    section('Your groups'),
                    if (_mine.isEmpty)
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 4),
                        child: Text(
                          "You're not in any groups yet. Join one nearby, or start one for your estate, "
                          'church, chama or team.',
                          style: text.bodyMedium?.copyWith(color: Palette.muted),
                        ),
                      )
                    else
                      for (final group in _mine)
                        Padding(
                          padding: const EdgeInsets.only(bottom: 8),
                          child: GroupTile(group: group, onTap: () => context.go(Routes.group(group.id))),
                        ),
                    section(
                      'Nearby',
                      _radiusKm == null ? null : 'Within ${_km(_radiusKm!)} km of home',
                    ),
                    TextField(
                      controller: _search,
                      onChanged: _onSearch,
                      textInputAction: TextInputAction.search,
                      decoration: InputDecoration(
                        hintText: 'Search groups by name',
                        prefixIcon: Icon(Icons.search_rounded, color: Palette.muted),
                        suffixIcon: _search.text.isEmpty
                            ? null
                            : IconButton(
                                tooltip: 'Clear',
                                icon: const Icon(Icons.close_rounded),
                                onPressed: () {
                                  _search.clear();
                                  _onSearch('');
                                },
                              ),
                      ),
                    ),
                    SizedBox(
                      height: 52,
                      child: ListView(
                        scrollDirection: Axis.horizontal,
                        padding: const EdgeInsets.only(top: 10, bottom: 6),
                        children: [
                          GroupCategoryChip(
                            label: 'All',
                            selected: _category == null,
                            onTap: () {
                              setState(() => _category = null);
                              _discover();
                            },
                          ),
                          for (final entry in groupCategories.entries)
                            GroupCategoryChip(
                              label: entry.value,
                              selected: _category == entry.key,
                              onTap: () {
                                setState(() => _category = entry.key);
                                _discover();
                              },
                            ),
                        ],
                      ),
                    ),
                    if (nearby.isEmpty && !_loadingNearby)
                      Padding(
                        padding: const EdgeInsets.symmetric(horizontal: 4, vertical: 12),
                        child: Text(
                          filtered ? 'No groups match. Try another word, or all categories.' : 'No public groups nearby yet.',
                          style: text.bodyMedium?.copyWith(color: Palette.muted),
                        ),
                      ),
                    for (final group in nearby)
                      Padding(
                        padding: const EdgeInsets.only(bottom: 8),
                        child: GroupTile(group: group, onTap: () => context.go(Routes.group(group.id))),
                      ),
                    if (_loadingNearby)
                      Padding(
                        padding: const EdgeInsets.all(16),
                        child: Center(child: CircularProgressIndicator(color: Palette.primary)),
                      ),
                  ],
                ),
              ),
      ),
    );
  }

  static String _km(double value) =>
      value == value.roundToDouble() ? value.toStringAsFixed(0) : value.toStringAsFixed(1);
}
