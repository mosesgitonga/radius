import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:share_plus/share_plus.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/location.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../me_screen.dart';

// ---------------------------------------------------------------------------
// Download my data
// ---------------------------------------------------------------------------

/// The right of access: everything held about the account, as a file the
/// person keeps. The file goes straight to the share sheet; the app keeps no copy.
class MyDataScreen extends ConsumerStatefulWidget {
  const MyDataScreen({super.key});

  @override
  ConsumerState<MyDataScreen> createState() => _MyDataScreenState();
}

class _MyDataScreenState extends ConsumerState<MyDataScreen> {
  bool _busy = false;
  String? _error;

  Future<void> _export() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final json = await ref.read(accountRepositoryProvider).export();
      final today = DateTime.now().toIso8601String().substring(0, 10);
      final name = 'myradius-data-$today.json';
      await SharePlus.instance.share(ShareParams(
        files: [XFile.fromData(utf8.encode(json), mimeType: 'application/json', name: name)],
        fileNameOverrides: [name],
        subject: 'My MyRadius data',
      ));
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Download my data')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text(
            'A copy of everything MyRadius holds about you: your account, consents, posts, comments, '
            'listings, services, reviews, messages you sent, devices, notifications, and any moderation '
            'decisions about you.',
            style: text.bodyLarge,
          ),
          const SizedBox(height: 12),
          Text(
            'It also says what is not held: no location history, no photo metadata, and never who '
            'reported you.',
            style: text.bodyMedium?.copyWith(color: Palette.muted),
          ),
          const SizedBox(height: 16),
          const NotePanel(
            icon: Icons.lock_outline_rounded,
            text: 'The file holds your phone number and messages. Keep it somewhere private.',
          ),
          FormError(_error),
          const SizedBox(height: 20),
          BusyButton(label: 'Get my data', busy: _busy, onPressed: _export),
          const SizedBox(height: 8),
          Text('You can do this up to 5 times a day.', textAlign: TextAlign.center, style: text.bodySmall),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Delete my account
// ---------------------------------------------------------------------------

class DeleteAccountScreen extends ConsumerStatefulWidget {
  const DeleteAccountScreen({super.key});

  @override
  ConsumerState<DeleteAccountScreen> createState() => _DeleteAccountScreenState();
}

class _DeleteAccountScreenState extends ConsumerState<DeleteAccountScreen> {
  bool _understood = false;
  bool _busy = false;
  String? _error;

  Future<void> _delete() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final status = await ref.read(accountRepositoryProvider).requestDeletion();
      if (!mounted) return;
      setState(() => _busy = false);
      await showDialog<void>(
        context: context,
        barrierDismissible: false,
        builder: (dialog) => AlertDialog(
          title: const Text('Deletion requested'),
          content: Text(
            'Your account will be deleted on ${shortDate(status.purgeAfter)}. You have been signed out '
            'everywhere. To change your mind before then, sign in again and cancel.',
          ),
          actions: [TextButton(onPressed: () => Navigator.of(dialog).pop(), child: const Text('OK'))],
        ),
      );
      // The server has already ended every session, this one included.
      await ref.read(authControllerProvider.notifier).endedByServer();
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = error.message;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Delete my account')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text('What happens', style: text.titleMedium),
          const SizedBox(height: 8),
          for (final line in const [
            'You are signed out on every device straight away.',
            'For 30 days your account waits. Sign in again in that time and you can cancel.',
            'After that it is deleted for good: your profile, posts, comments, listings, services, '
            'reviews, messages you sent, photos and devices.',
            "Backups are kept for 14 days, so a copy lasts that much longer, and then it's gone.",
            'A report against something you wrote is kept until a moderator has dealt with it.',
          ])
            Padding(
              padding: const EdgeInsets.only(bottom: 8),
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Padding(
                    padding: EdgeInsets.only(top: 6, right: 10),
                    child: Icon(Icons.circle, size: 6, color: Palette.inkSoft),
                  ),
                  Expanded(child: Text(line, style: text.bodyMedium)),
                ],
              ),
            ),
          const SizedBox(height: 8),
          const NotePanel(
            icon: Icons.download_outlined,
            text: 'Want a copy first? Use Download my data before you delete.',
          ),
          const SizedBox(height: 12),
          CheckboxListTile(
            contentPadding: EdgeInsets.zero,
            controlAffinity: ListTileControlAffinity.leading,
            value: _understood,
            onChanged: (value) => setState(() => _understood = value ?? false),
            title: const Text('I understand my account will be deleted after 30 days'),
          ),
          FormError(_error),
          const SizedBox(height: 12),
          FilledButton(
            onPressed: _understood && !_busy ? _delete : null,
            style: FilledButton.styleFrom(backgroundColor: Palette.danger),
            child: _busy
                ? const SizedBox.square(
                    dimension: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Palette.white))
                : const Text('Delete my account'),
          ),
        ],
      ),
    );
  }
}

// ---------------------------------------------------------------------------
// Home area
// ---------------------------------------------------------------------------

/// Moving home: the position is read once and sent once, as at sign-up. The
/// server allows a change about once a fortnight.
class MoveHomeScreen extends ConsumerStatefulWidget {
  const MoveHomeScreen({super.key});

  @override
  ConsumerState<MoveHomeScreen> createState() => _MoveHomeScreenState();
}

class _MoveHomeScreenState extends ConsumerState<MoveHomeScreen> {
  bool _busy = false;
  String? _error;
  String? _moved;

  Future<void> _move() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final reading = await ref.read(locationReaderProvider).readHere();
      final area = await ref.read(authRepositoryProvider).setHome(reading);
      await ref.read(authControllerProvider.notifier).refreshMe();
      setState(() => _moved = area.label);
    } on LocationProblem catch (problem) {
      setState(() => _error = problem.message);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    final provisional = me?.homeProvisional ?? false;
    final next = me?.nextHomeChangeAllowedAt;
    final waiting = !provisional && next != null && next.isAfter(DateTime.now());
    return Scaffold(
      appBar: AppBar(title: const Text('Home area')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text(_moved ?? me?.areaLabel?.label ?? 'Not set', style: text.headlineSmall),
          const SizedBox(height: 6),
          Text(
            _moved != null
                ? 'Home is now $_moved. Your feed, the marketplace and services follow it.'
                : provisional
                    ? "You're using MyRadius from here for now. When you're at home, set it here"
                        "${me?.homeConfirmBy == null ? '' : ' by ${shortDate(me!.homeConfirmBy!)}'}."
                    : 'Your feed, the marketplace and services are built around your home area.',
            style: text.bodyLarge?.copyWith(color: Palette.muted),
          ),
          const SizedBox(height: 16),
          const NotePanel(
            icon: Icons.shield_outlined,
            text: 'We keep only the area, never your exact spot. Moving is possible about once a fortnight, '
                'so nobody can hop between neighbourhoods.',
          ),
          if (waiting && _moved == null) ...[
            const SizedBox(height: 12),
            Text('You can move again from ${shortDate(next)}.', style: text.bodyMedium),
          ],
          FormError(_error),
          const SizedBox(height: 20),
          if (_moved == null)
            BusyButton(
              label: provisional ? "I'm at home now: use my location" : "I've moved: use my location",
              busy: _busy,
              onPressed: waiting ? null : _move,
            ),
        ],
      ),
    );
  }
}
