import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../auth/auth_controller.dart';

/// The account, as far as slice 1 goes: who you are, where home is, and
/// signing out. Standing, settings and data rights come in the Me slice.
class MeScreen extends ConsumerWidget {
  const MeScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    if (me == null) return const Scaffold();

    final initials = me.displayName
        .split(RegExp(r'\s+'))
        .where((part) => part.isNotEmpty)
        .take(2)
        .map((part) => part[0].toUpperCase())
        .join();

    return Scaffold(
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 24, 16, 24),
          children: [
            Row(
              children: [
                CircleAvatar(
                  radius: 32,
                  backgroundColor: Palette.trustTint,
                  child: Text(initials, style: text.titleLarge),
                ),
                const SizedBox(width: 14),
                Expanded(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Text(me.displayName, style: text.headlineSmall),
                      const SizedBox(height: 2),
                      Text(me.areaLabel?.label ?? 'Home area set',
                          style: text.bodyMedium?.copyWith(color: Palette.muted)),
                    ],
                  ),
                ),
              ],
            ),
            if (me.isVerifiedNeighbour) ...[
              const SizedBox(height: 12),
              const Align(alignment: Alignment.centerLeft, child: NeighbourBadge(long: true)),
            ],
            const SizedBox(height: 32),
            OutlinedButton.icon(
              onPressed: () => ref.read(authControllerProvider.notifier).signOut(),
              icon: const Icon(Icons.logout_rounded),
              label: const Text('Sign out'),
            ),
          ],
        ),
      ),
    );
  }
}
