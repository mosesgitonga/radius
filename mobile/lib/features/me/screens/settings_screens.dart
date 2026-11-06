import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../account_models.dart';
import '../me_screen.dart';

/// Loads something once, shows a spinner, then the page; or the server's message.
class _Loaded<T> extends StatelessWidget {
  const _Loaded({required this.value, required this.error, required this.builder});

  final T? value;
  final String? error;
  final Widget Function(T value) builder;

  @override
  Widget build(BuildContext context) {
    if (value != null) return builder(value as T);
    if (error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Text(error!, textAlign: TextAlign.center, style: Theme.of(context).textTheme.bodyLarge),
        ),
      );
    }
    return Center(child: CircularProgressIndicator(color: Palette.primary));
  }
}

void _toast(BuildContext context, String message) =>
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));

// ---------------------------------------------------------------------------
// Notifications
// ---------------------------------------------------------------------------

class NotificationSettingsScreen extends ConsumerStatefulWidget {
  const NotificationSettingsScreen({super.key});

  @override
  ConsumerState<NotificationSettingsScreen> createState() => _NotificationSettingsScreenState();
}

class _NotificationSettingsScreenState extends ConsumerState<NotificationSettingsScreen> {
  List<NotificationPreference>? _preferences;
  String? _error;

  @override
  void initState() {
    super.initState();
    ref.read(accountRepositoryProvider).preferences().then(
      (value) {
        // In the order people think of them, messages first, not the server's
        // alphabetical one. Kinds the app does not know go last.
        int rank(NotificationPreference p) {
          final index = notificationNames.keys.toList().indexOf(p.type);
          return index < 0 ? 1 << 20 : index;
        }

        if (mounted) setState(() => _preferences = [...value]..sort((a, b) => rank(a).compareTo(rank(b))));
      },
      onError: (Object error) {
        if (mounted) setState(() => _error = error is ApiException ? error.message : '$error');
      },
    );
  }

  /// Shown at once, put back if the server refuses.
  Future<void> _change(NotificationPreference before, NotificationPreference after) async {
    setState(() => _preferences = [for (final p in _preferences!) p.type == before.type ? after : p]);
    try {
      await ref.read(accountRepositoryProvider).setPreference(after);
    } on ApiException catch (error) {
      if (!mounted) return;
      setState(() => _preferences = [for (final p in _preferences!) p.type == before.type ? before : p]);
      _toast(context, error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Notifications')),
      body: _Loaded<List<NotificationPreference>>(
        value: _preferences,
        error: _error,
        builder: (preferences) => ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            const NotePanel(
              icon: Icons.info_outline_rounded,
              text: 'Phone notifications (push) arrive in a later build. Your choices here are kept and '
                  'apply as soon as they do.',
            ),
            const SizedBox(height: 12),
            for (final p in preferences)
              Container(
                margin: const EdgeInsets.only(bottom: 10),
                padding: const EdgeInsets.fromLTRB(14, 10, 6, 6),
                decoration: BoxDecoration(color: Palette.surface, borderRadius: BorderRadius.circular(14)),
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.start,
                  children: [
                    Text(notificationName(p.type), style: text.titleSmall),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      dense: true,
                      title: const Text('On my phone'),
                      value: p.push,
                      onChanged: (on) => _change(p, p.copyWith(push: on)),
                    ),
                    SwitchListTile(
                      contentPadding: EdgeInsets.zero,
                      dense: true,
                      title: const Text('In the app'),
                      value: p.inApp,
                      onChanged: (on) => _change(p, p.copyWith(inApp: on)),
                    ),
                    if (p.type == 'message' || p.type == 'message_request')
                      SwitchListTile(
                        contentPadding: EdgeInsets.zero,
                        dense: true,
                        title: const Text('Show the message on the lock screen'),
                        subtitle: const Text('Off by default: anyone holding the phone could read it.'),
                        value: p.preview,
                        onChanged: (on) => _change(p, p.copyWith(preview: on)),
                      ),
                  ],
                ),
              ),
          ],
        ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Privacy and consent
// ---------------------------------------------------------------------------

class PrivacyScreen extends ConsumerStatefulWidget {
  const PrivacyScreen({super.key});

  @override
  ConsumerState<PrivacyScreen> createState() => _PrivacyScreenState();
}

class _PrivacyScreenState extends ConsumerState<PrivacyScreen> {
  List<Consent>? _consents;
  String? _error;

  @override
  void initState() {
    super.initState();
    ref.read(accountRepositoryProvider).consents().then(
      (value) {
        if (mounted) setState(() => _consents = value);
      },
      onError: (Object error) {
        if (mounted) setState(() => _error = error is ApiException ? error.message : '$error');
      },
    );
  }

  bool get _showInNearby {
    final auth = ref.watch(authControllerProvider);
    return auth is AuthSignedIn && auth.me.showInNearby;
  }

  Future<void> _setShowInNearby(bool show) async {
    try {
      final me = await ref.read(accountRepositoryProvider).setShowInNearby(show);
      ref.read(authControllerProvider.notifier).replaceMe(me);
    } on ApiException catch (error) {
      if (mounted) _toast(context, error.message);
    }
  }

  Future<void> _setMarketing(bool granted) async {
    try {
      final updated = await ref.read(accountRepositoryProvider).setMarketing(granted);
      if (mounted) setState(() => _consents = [for (final c in _consents!) c.type == updated.type ? updated : c]);
    } on ApiException catch (error) {
      if (mounted) _toast(context, error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Privacy and consent')),
      body: _Loaded<List<Consent>>(
        value: _consents,
        error: _error,
        builder: (consents) {
          final marketing = consents.where((c) => c.type == 'marketing').firstOrNull;
          return ListView(
            padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
            children: [
              Text('What you agreed to', style: text.titleMedium),
              const SizedBox(height: 4),
              Text(
                'Each is recorded separately, with the version you agreed to, as the Data Protection Act requires.',
                style: text.bodySmall,
              ),
              const SizedBox(height: 12),
              for (final c in consents.where((c) => !c.optional))
                ListTile(
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(c.granted ? Icons.check_circle_rounded : Icons.cancel_outlined,
                      color: c.granted ? Palette.trust : Palette.muted),
                  title: Text(consentNames[c.type] ?? c.type),
                  subtitle: Text('Version ${c.version} · ${shortDate(c.recordedAt)}'),
                ),
              const SizedBox(height: 8),
              const NotePanel(
                icon: Icons.info_outline_rounded,
                text: 'These are needed for MyRadius to work. To withdraw them, delete your account.',
              ),
              const SizedBox(height: 20),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(consentNames['marketing']!),
                subtitle: const Text('Optional. You can change this at any time.'),
                value: marketing?.granted ?? false,
                onChanged: _setMarketing,
              ),
              const SizedBox(height: 20),
              Text('Being found', style: text.titleMedium),
              const SizedBox(height: 4),
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Show me in People nearby'),
                subtitle: const Text(
                  'Neighbours whose feed reaches your home see your name, photo, bio and area, '
                  'never where you live. Only once your home is proven.',
                ),
                value: _showInNearby,
                onChanged: _setShowInNearby,
              ),
              const SizedBox(height: 24),
              Text(
                'Area names and boundaries: map data © OpenStreetMap contributors, '
                'available under the Open Database Licence.',
                style: text.bodySmall,
              ),
            ],
          );
        },
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Blocked people
// ---------------------------------------------------------------------------

class BlockedScreen extends ConsumerStatefulWidget {
  const BlockedScreen({super.key});

  @override
  ConsumerState<BlockedScreen> createState() => _BlockedScreenState();
}

class _BlockedScreenState extends ConsumerState<BlockedScreen> {
  List<BlockedPerson>? _blocked;
  String? _error;

  @override
  void initState() {
    super.initState();
    ref.read(accountRepositoryProvider).blocks().then(
      (value) {
        if (mounted) setState(() => _blocked = value);
      },
      onError: (Object error) {
        if (mounted) setState(() => _error = error is ApiException ? error.message : '$error');
      },
    );
  }

  Future<void> _unblock(BlockedPerson person) async {
    final sure = await showDialog<bool>(
      context: context,
      builder: (dialog) => AlertDialog(
        title: Text('Unblock ${person.displayName}?'),
        content: const Text('You will see each other again in MyRadius, and they can message you.'),
        actions: [
          TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Cancel')),
          TextButton(onPressed: () => Navigator.of(dialog).pop(true), child: const Text('Unblock')),
        ],
      ),
    );
    if (sure != true) return;
    try {
      await ref.read(accountRepositoryProvider).unblock(person.userId);
      if (mounted) setState(() => _blocked = _blocked!.where((b) => b.userId != person.userId).toList());
    } on ApiException catch (error) {
      if (mounted) _toast(context, error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Blocked people')),
      body: _Loaded<List<BlockedPerson>>(
        value: _blocked,
        error: _error,
        builder: (blocked) => blocked.isEmpty
            ? Center(
                child: Padding(
                  padding: const EdgeInsets.all(32),
                  child: Text(
                    "You haven't blocked anyone. Block someone from a chat's menu.",
                    textAlign: TextAlign.center,
                    style: text.bodyLarge?.copyWith(color: Palette.muted),
                  ),
                ),
              )
            : ListView(
                padding: const EdgeInsets.fromLTRB(8, 8, 8, 32),
                children: [
                  Padding(
                    padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
                    child: Text(
                      "You and they can't see each other anywhere in MyRadius. They aren't told.",
                      style: text.bodySmall,
                    ),
                  ),
                  for (final person in blocked)
                    ListTile(
                      title: Text(person.displayName),
                      subtitle: Text('Blocked ${shortDate(person.blockedAt)}'),
                      trailing: TextButton(onPressed: () => _unblock(person), child: const Text('Unblock')),
                    ),
                ],
              ),
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Where I'm signed in
// ---------------------------------------------------------------------------

class SessionsScreen extends ConsumerStatefulWidget {
  const SessionsScreen({super.key});

  @override
  ConsumerState<SessionsScreen> createState() => _SessionsScreenState();
}

class _SessionsScreenState extends ConsumerState<SessionsScreen> {
  List<DeviceSession>? _sessions;
  String? _error;
  bool _busy = false;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    try {
      final sessions = await ref.read(accountRepositoryProvider).sessions();
      if (mounted) setState(() => _sessions = sessions);
    } on ApiException catch (error) {
      if (mounted) setState(() => _error = error.message);
    }
  }

  Future<void> _signOutOthers() async {
    setState(() => _busy = true);
    try {
      final count = await ref.read(accountRepositoryProvider).signOutOthers();
      if (!mounted) return;
      _toast(context, count == 1 ? 'Signed out 1 other device.' : 'Signed out $count other devices.');
      await _load();
    } on ApiException catch (error) {
      if (mounted) _toast(context, error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text("Where I'm signed in")),
      body: _Loaded<List<DeviceSession>>(
        value: _sessions,
        error: _error,
        builder: (sessions) => ListView(
          padding: const EdgeInsets.fromLTRB(8, 8, 8, 32),
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(8, 0, 8, 8),
              child: Text("If you don't recognise a device, sign out everywhere else.", style: text.bodySmall),
            ),
            for (final s in sessions)
              ListTile(
                leading: Icon(s.platform == 'ios' || s.platform == 'android'
                    ? Icons.smartphone_rounded
                    : Icons.devices_other_rounded),
                title: Text(s.isCurrent ? '${s.label} · this phone' : s.label),
                subtitle: Text('Signed in ${shortDate(s.createdAt)} · last used ${shortDate(s.lastUsedAt)}'),
              ),
            const SizedBox(height: 16),
            Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: BusyButton(
                label: 'Sign out everywhere else',
                busy: _busy,
                onPressed: sessions.where((s) => !s.isCurrent).isEmpty ? null : _signOutOthers,
              ),
            ),
          ],
        ),
      ),
    );
  }
}
