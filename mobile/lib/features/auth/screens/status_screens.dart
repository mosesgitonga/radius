import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';

/// Shown for the moment it takes to restore a stored session at launch.
class RestoringScreen extends StatelessWidget {
  const RestoringScreen({super.key});

  @override
  Widget build(BuildContext context) {
    return const Scaffold(
      body: Center(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            RadiusMark(size: 64),
            SizedBox(height: 24),
            SizedBox.square(
              dimension: 22,
              child: CircularProgressIndicator(strokeWidth: 2.5, color: Palette.primary),
            ),
          ],
        ),
      ),
    );
  }
}

/// A session is stored but the server can't be reached to check it.
class OfflineScreen extends ConsumerWidget {
  const OfflineScreen({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Wordmark(),
              const Spacer(),
              const Icon(Icons.wifi_off_rounded, size: 40, color: Palette.primary),
              const SizedBox(height: 16),
              Text("You're offline", style: text.headlineSmall),
              const SizedBox(height: 8),
              Text(
                "We can't reach MyRadius right now. Check your data or Wi-Fi and try again.",
                style: text.bodyLarge?.copyWith(color: Palette.muted),
              ),
              const Spacer(),
              FilledButton(
                onPressed: () => ref.read(authControllerProvider.notifier).retry(),
                child: const Text('Try again'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
