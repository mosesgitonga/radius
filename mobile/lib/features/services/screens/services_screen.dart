import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../contact.dart';
import '../service_models.dart';
import '../services_controller.dart';
import '../widgets/service_cards.dart';

/// Services & jobs: what neighbours offer, what they need, and who is hiring.
class ServicesScreen extends ConsumerStatefulWidget {
  const ServicesScreen({super.key});

  @override
  ConsumerState<ServicesScreen> createState() => _ServicesScreenState();
}

class _ServicesScreenState extends ConsumerState<ServicesScreen> {
  final _search = TextEditingController();
  final _scroll = ScrollController();
  Timer? _debounce;

  ServicesController get _controller => ref.read(servicesControllerProvider.notifier);

  @override
  void initState() {
    super.initState();
    _scroll.addListener(() {
      if (_scroll.position.pixels > _scroll.position.maxScrollExtent - 600) _controller.loadMore();
    });
  }

  @override
  void dispose() {
    _debounce?.cancel();
    _search.dispose();
    _scroll.dispose();
    super.dispose();
  }

  /// Searches once typing pauses, not on every key.
  void _onSearch(String value) {
    _debounce?.cancel();
    _debounce = Timer(const Duration(milliseconds: 400), () => _controller.setQuery(value));
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final state = ref.watch(servicesControllerProvider);

    return Scaffold(
      appBar: AppBar(
        title: const Text('Services & jobs'),
        actions: [
          IconButton(
            tooltip: 'Your posts',
            onPressed: () => context.go(Routes.myServices),
            icon: const Icon(Icons.inventory_2_outlined),
          ),
          Padding(
            padding: const EdgeInsets.only(right: 12),
            child: FilledButton.icon(
              onPressed: () => context.push(Routes.newService(state.kind)),
              style: FilledButton.styleFrom(minimumSize: const Size(0, 40), padding: const EdgeInsets.symmetric(horizontal: 14)),
              icon: const Icon(Icons.add_rounded, size: 18),
              label: const Text('Post'),
            ),
          ),
        ],
      ),
      body: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 4, 16, 0),
            child: SegmentedButton<ServiceKind>(
              showSelectedIcon: false,
              segments: [for (final kind in ServiceKind.values) ButtonSegment(value: kind, label: Text(kind.label))],
              selected: {state.kind},
              onSelectionChanged: (value) => _controller.setKind(value.single),
            ),
          ),
          Padding(
            padding: const EdgeInsets.fromLTRB(16, 12, 16, 0),
            child: TextField(
              controller: _search,
              onChanged: (value) {
                setState(() {});
                _onSearch(value);
              },
              textInputAction: TextInputAction.search,
              decoration: InputDecoration(
                hintText: switch (state.kind) {
                  ServiceKind.offering => 'Plumber, mama fua, tutor…',
                  ServiceKind.seeking => 'What do neighbours need?',
                  ServiceKind.hiring => 'Shop attendant, driver…',
                },
                prefixIcon: Icon(Icons.search_rounded, color: Palette.muted),
                suffixIcon: _search.text.isEmpty
                    ? null
                    : IconButton(
                        tooltip: 'Clear',
                        icon: const Icon(Icons.close_rounded),
                        onPressed: () {
                          _search.clear();
                          _controller.setQuery('');
                          setState(() {});
                        },
                      ),
              ),
            ),
          ),
          SizedBox(
            height: 52,
            child: ListView(
              scrollDirection: Axis.horizontal,
              padding: const EdgeInsets.fromLTRB(16, 10, 16, 6),
              children: [
                _CategoryChip(
                  label: 'All',
                  selected: state.category == null,
                  onTap: () => _controller.setCategory(null),
                ),
                for (final entry in serviceCategories.entries)
                  _CategoryChip(
                    label: entry.value,
                    selected: state.category == entry.key,
                    onTap: () => _controller.setCategory(entry.key),
                  ),
              ],
            ),
          ),
          Expanded(
            child: RefreshIndicator(
              color: Palette.primary,
              onRefresh: _controller.refresh,
              child: _list(state, text),
            ),
          ),
        ],
      ),
    );
  }

  Widget _list(ServicesState state, TextTheme text) {
    final header = <Widget>[
      if (state.kind == ServiceKind.hiring) const JobSafetyNote(),
      if (state.radiusKm != null)
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: 4),
          child: Text('Within ${_km(state.radiusKm!)} km of home', style: text.bodySmall),
        ),
    ];
    if (state.loading && state.posts.isEmpty) {
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        children: [
          SizedBox(height: 120),
          Center(child: CircularProgressIndicator(color: Palette.primary)),
        ],
      );
    }
    if (state.posts.isEmpty) {
      final (title, body) = state.error != null
          ? ("Couldn't load these", state.error!.message)
          : state.filtered
              ? ('Nothing matches', 'Try another word, or all categories.')
              : switch (state.kind) {
                  ServiceKind.offering => ('No one is offering yet', 'Offer what you do, and neighbours will find you.'),
                  ServiceKind.seeking => ('No requests nearby', 'When a neighbour needs a hand, it shows here.'),
                  ServiceKind.hiring => ('No jobs nearby', 'Jobs posted near you show here.'),
                };
      return ListView(
        physics: const AlwaysScrollableScrollPhysics(),
        padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
        children: [
          ...header,
          const SizedBox(height: 48),
          const Center(child: RadiusMark(size: 48)),
          const SizedBox(height: 16),
          Text(title, textAlign: TextAlign.center, style: text.titleLarge),
          const SizedBox(height: 8),
          Text(body, textAlign: TextAlign.center, style: text.bodyMedium?.copyWith(color: Palette.muted)),
        ],
      );
    }
    return ListView(
      controller: _scroll,
      physics: const AlwaysScrollableScrollPhysics(),
      padding: const EdgeInsets.fromLTRB(12, 4, 12, 24),
      children: [
        for (final widget in header) Padding(padding: const EdgeInsets.only(bottom: 10), child: widget),
        for (final post in state.posts)
          Padding(
            padding: const EdgeInsets.only(bottom: 10),
            child: ServiceCard(
              post: post,
              onOpen: () => context.go(Routes.service(post.id)),
              onHelp: () => contactAuthor(context, ref, post),
            ),
          ),
        if (state.loadingMore)
          Padding(
            padding: EdgeInsets.all(16),
            child: Center(child: CircularProgressIndicator(color: Palette.primary)),
          ),
      ],
    );
  }

  static String _km(double value) =>
      value == value.roundToDouble() ? value.toStringAsFixed(0) : value.toStringAsFixed(1);
}

class _CategoryChip extends StatelessWidget {
  const _CategoryChip({required this.label, required this.selected, required this.onTap});

  final String label;
  final bool selected;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        label: Text(label),
        selected: selected,
        onSelected: (_) => onTap(),
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
