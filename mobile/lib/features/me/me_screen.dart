import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/router.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../auth/auth_controller.dart';
import '../auth/auth_models.dart';
import 'account_models.dart';

/// Settings, reached from my profile: where I stand, my things, and my
/// settings and rights.
class AccountScreen extends ConsumerStatefulWidget {
  const AccountScreen({super.key});

  @override
  ConsumerState<AccountScreen> createState() => _AccountScreenState();
}

class _AccountScreenState extends ConsumerState<AccountScreen> {
  Standing? _standing;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final standing = await ref.read(accountRepositoryProvider).standing();
      if (mounted) setState(() => _standing = standing);
      await ref.read(authControllerProvider.notifier).refreshMe();
    } on ApiException {
      // Shown as it was; pulling down tries again.
    }
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  Future<void> _cancelDeletion() async {
    try {
      await ref.read(accountRepositoryProvider).cancelDeletion();
      await ref.read(authControllerProvider.notifier).refreshMe();
      _toast('Deletion cancelled. Your account stays.');
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    if (me == null) return const Scaffold();

    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: SafeArea(
        top: false,
        child: RefreshIndicator(
          color: Palette.primary,
          onRefresh: _load,
          child: ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              if (me.deletionPending) ...[
                _DeletionBanner(requestedAt: me.deletionRequestedAt!, onCancel: _cancelDeletion),
                const SizedBox(height: 14),
              ],
              _StandingCard(me: me, standing: _standing),
              const SizedBox(height: 14),
              _Group(rows: [
                _Row(label: 'Your listings', onTap: () => context.go(Routes.myListings)),
                _Row(label: 'Saved listings', onTap: () => context.go(Routes.savedListings)),
                _Row(label: 'Your service posts', onTap: () => context.go(Routes.myServices)),
              ]),
              const SizedBox(height: 14),
              _Group(rows: [
                _Row(
                  label: 'Home area',
                  detail: me.areaLabel?.label,
                  onTap: () => context.go(Routes.moveHome),
                ),
                _Row(label: 'Notifications', onTap: () => context.go(Routes.notificationSettings)),
                _Row(label: 'Privacy and consent', onTap: () => context.go(Routes.privacy)),
                _Row(label: 'Blocked people', onTap: () => context.go(Routes.blocked)),
                _Row(label: "Where I'm signed in", onTap: () => context.go(Routes.sessions)),
                _Row(label: 'Download my data', onTap: () => context.go(Routes.myData)),
                if (!me.deletionPending)
                  _Row(label: 'Delete my account', danger: true, onTap: () => context.go(Routes.deleteAccount)),
              ]),
              const SizedBox(height: 20),
              OutlinedButton.icon(
                onPressed: () => ref.read(authControllerProvider.notifier).signOut(),
                icon: const Icon(Icons.logout_rounded),
                label: const Text('Sign out'),
              ),
              if (me.phoneNumber != null) ...[
                const SizedBox(height: 16),
                Text(
                  'Signed in as ${me.phoneNumber}'
                  '${me.memberSince == null ? '' : ' · on MyRadius since ${_monthYear(me.memberSince!)}'}',
                  textAlign: TextAlign.center,
                  style: text.bodySmall,
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }

  static String _monthYear(DateTime date) {
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September',
      'October', 'November', 'December'];
    final local = date.toLocal();
    return '${months[local.month - 1]} ${local.year}';
  }
}

/// "12 Oct": a short date, as everywhere in the app.
String shortDate(DateTime date) {
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  final local = date.toLocal();
  return '${local.day} ${months[local.month - 1]} ${local.year}';
}

class _DeletionBanner extends StatelessWidget {
  const _DeletionBanner({required this.requestedAt, required this.onCancel});

  final DateTime requestedAt;
  final VoidCallback onCancel;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(color: Palette.glowTint, borderRadius: BorderRadius.circular(14)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('Your account is due to be deleted', style: text.titleSmall),
          const SizedBox(height: 4),
          Text(
            'You asked for this on ${shortDate(requestedAt)}. Until the grace period ends you can '
            'read, but not post or message. Cancel to keep your account.',
            style: text.bodySmall?.copyWith(color: Palette.ink),
          ),
          const SizedBox(height: 8),
          FilledButton(onPressed: onCancel, child: const Text('Cancel deletion')),
        ],
      ),
    );
  }
}

/// The badge, and anything the moderators have decided about the account.
class _StandingCard extends StatelessWidget {
  const _StandingCard({required this.me, required this.standing});

  final Me me;
  final Standing? standing;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final s = standing;
    final lines = <Widget>[];
    if (s != null && s.suspended) {
      lines.add(_Notice(
        title: 'Suspended',
        body: s.suspendedUntil == null
            ? 'You can read, but not post or message, until a moderator lifts it.'
            : 'You can read, but not post or message, until ${shortDate(s.suspendedUntil!)}.',
      ));
    }
    if (s != null && s.restricted) {
      lines.add(_Notice(
        title: 'Restricted',
        body: [
          if (s.restrictionReason != null) s.restrictionReason!,
          s.restrictedUntil == null
              ? 'New posts are checked by a moderator first.'
              : 'New posts are checked by a moderator first, until ${shortDate(s.restrictedUntil!)}.',
        ].join(' '),
      ));
    }
    for (final warning in s?.warnings ?? const <Warning>[]) {
      lines.add(_Notice(title: 'Warning from the moderators · ${shortDate(warning.at)}', body: warning.reason));
    }

    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(color: Palette.white, borderRadius: BorderRadius.circular(16)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          if (me.isVerifiedNeighbour) ...[
            const NeighbourBadge(long: true),
            const SizedBox(height: 8),
            Text(
              'Your reports count fully, and you can vouch for neighbours you know.',
              style: text.bodyMedium?.copyWith(color: Palette.inkSoft),
            ),
          ] else ...[
            Text('Becoming a verified neighbour', style: text.titleSmall),
            const SizedBox(height: 6),
            Text(
              'The badge comes from being home in your area on most days for a couple of weeks, '
              'or from verified neighbours vouching for you.'
              '${s == null ? '' : ' ${_vouches(s.vouchesReceived)}'}',
              style: text.bodyMedium?.copyWith(color: Palette.inkSoft),
            ),
          ],
          for (final line in lines) ...[const SizedBox(height: 12), line],
        ],
      ),
    );
  }
}

String _vouches(int count) => switch (count) {
      0 => 'No one has vouched for you yet.',
      1 => '1 neighbour has vouched for you so far.',
      _ => '$count neighbours have vouched for you so far.',
    };

class _Notice extends StatelessWidget {
  const _Notice({required this.title, required this.body});

  final String title;
  final String body;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(color: Palette.glowTint, borderRadius: BorderRadius.circular(10)),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(title, style: text.titleSmall?.copyWith(fontSize: 14)),
          const SizedBox(height: 2),
          Text(body, style: text.bodySmall?.copyWith(color: Palette.ink)),
        ],
      ),
    );
  }
}

class _Row {
  const _Row({required this.label, required this.onTap, this.detail, this.danger = false});
  final String label;
  final String? detail;
  final bool danger;
  final VoidCallback onTap;
}

class _Group extends StatelessWidget {
  const _Group({required this.rows});

  final List<_Row> rows;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Material(
      color: Palette.white,
      borderRadius: BorderRadius.circular(16),
      clipBehavior: Clip.antiAlias,
      child: Column(
        children: [
          for (var i = 0; i < rows.length; i++) ...[
            if (i > 0) const Divider(height: 1, color: Palette.sand),
            ListTile(
              onTap: rows[i].onTap,
              title: Text(
                rows[i].label,
                style: text.bodyLarge?.copyWith(color: rows[i].danger ? Palette.danger : Palette.ink),
              ),
              trailing: Row(
                mainAxisSize: MainAxisSize.min,
                children: [
                  if (rows[i].detail != null)
                    Text(rows[i].detail!, style: text.bodyMedium?.copyWith(color: Palette.muted)),
                  const Icon(Icons.chevron_right_rounded, color: Palette.muted),
                ],
              ),
            ),
          ],
        ],
      ),
    );
  }
}
