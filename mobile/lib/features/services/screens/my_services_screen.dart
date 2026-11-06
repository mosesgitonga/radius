import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../service_models.dart';

/// My service posts: open, closed, expired and held, newest first.
class MyServicesScreen extends ConsumerStatefulWidget {
  const MyServicesScreen({super.key});

  @override
  ConsumerState<MyServicesScreen> createState() => _MyServicesScreenState();
}

class _MyServicesScreenState extends ConsumerState<MyServicesScreen> {
  final List<ServicePost> _posts = [];
  String? _cursor;
  bool _loading = true;
  String? _error;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load({bool more = false}) async {
    try {
      final page = await ref.read(serviceRepositoryProvider).mine(cursor: more ? _cursor : null);
      if (!mounted) return;
      setState(() {
        if (!more) _posts.clear();
        _posts.addAll(page.items);
        _cursor = page.nextCursor;
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

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Your posts')),
      floatingActionButton: FloatingActionButton.extended(
        onPressed: () => context.push(Routes.newService(ServiceKind.offering)),
        backgroundColor: Palette.primary,
        foregroundColor: Palette.onPrimary,
        icon: const Icon(Icons.add_rounded),
        label: const Text('New post'),
      ),
      body: RefreshIndicator(
        color: Palette.primary,
        onRefresh: _load,
        child: _loading
            ? ListView(children: [
                SizedBox(height: 120),
                Center(child: CircularProgressIndicator(color: Palette.primary)),
              ])
            : ListView(
                physics: const AlwaysScrollableScrollPhysics(),
                padding: const EdgeInsets.fromLTRB(12, 4, 12, 96),
                children: [
                  if (_posts.isEmpty) ...[
                    const SizedBox(height: 64),
                    const Center(child: RadiusMark(size: 48)),
                    const SizedBox(height: 16),
                    Text(_error ?? 'Nothing posted yet', textAlign: TextAlign.center, style: text.titleLarge),
                    const SizedBox(height: 8),
                    Text(
                      'Offer what you do, ask for a hand, or post a job.',
                      textAlign: TextAlign.center,
                      style: text.bodyMedium?.copyWith(color: Palette.muted),
                    ),
                  ],
                  for (final post in _posts)
                    Padding(
                      padding: const EdgeInsets.only(bottom: 8),
                      child: Material(
                        color: Palette.surface,
                        borderRadius: BorderRadius.circular(14),
                        child: ListTile(
                          shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(14)),
                          onTap: () => context.go(Routes.service(post.id)),
                          title: Text(post.title, maxLines: 1, overflow: TextOverflow.ellipsis),
                          subtitle: Text('${post.kind.label} · ${post.categoryName}'),
                          trailing: _Status(post),
                        ),
                      ),
                    ),
                  if (_cursor != null)
                    TextButton(onPressed: () => _load(more: true), child: const Text('More')),
                ],
              ),
      ),
    );
  }
}

class _Status extends StatelessWidget {
  const _Status(this.post);

  final ServicePost post;

  @override
  Widget build(BuildContext context) {
    final (label, bg, ink) = post.isHeld
        ? ('Held', Palette.glowTint, Palette.primaryDark)
        : post.isOpen
            ? ('Open', Palette.trustTint, Palette.trustInk)
            : ('Closed', Palette.sand, Palette.inkSoft);
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
      child: Text(label, style: TextStyle(color: ink, fontSize: 12.5, fontWeight: FontWeight.w600)),
    );
  }
}
