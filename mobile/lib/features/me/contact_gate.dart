import 'dart:math';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../auth/auth_controller.dart';

/// Whether I may start a conversation with someone I haven't talked to. When I
/// may not yet, explains why and returns false (decision M.7). The server holds
/// the same rule; this says it before a chat is opened for nothing.
Future<bool> mayContactStrangers(BuildContext context, WidgetRef ref) async {
  final auth = ref.read(authControllerProvider);
  if (auth is! AuthSignedIn || auth.me.canContactStrangers) return true;
  final needed = auth.me.contactUnlockDays;
  final window = auth.me.contactUnlockWindowDays;
  final soFar = min(auth.me.presenceDaysThisWeek, needed);
  await showDialog<void>(
    context: context,
    builder: (dialog) => AlertDialog(
      title: const Text('Settle in first'),
      content: Text(
        "To keep neighbourhoods safe, you can start conversations with people once you've been at home "
        'in your area on $needed of the last $window days. So far: $soFar of $needed.\n\n'
        'Until then you can post, comment and mark posts helpful, and reply to anyone who writes to you. '
        'Hoodora checks once a day while the app is open.',
      ),
      actions: [TextButton(onPressed: () => Navigator.of(dialog).pop(), child: const Text('OK'))],
    ),
  );
  return false;
}
