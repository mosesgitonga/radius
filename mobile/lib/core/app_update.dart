import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:url_launcher/url_launcher.dart';

import 'config.dart';
import 'providers.dart';
import 'theme.dart';
import 'widgets.dart';

/// Whether this build should update (decision M.12).
///
/// Most changes reach phones over the air as code patches and never change the
/// build number. This covers the rest: a build older than the server's oldest
/// supported one must update, and one older than the latest may.
class AppUpdate {
  const AppUpdate({
    required this.required,
    required this.available,
    this.url,
    this.message,
    this.latest,
    this.dismissed = false,
  });

  /// Decides from the server's answer and this build's number.
  factory AppUpdate.decide(Map<String, dynamic> json, {required int build}) {
    final latest = json['latest_build'] as int?;
    final minimum = json['min_supported_build'] as int?;
    return AppUpdate(
      required: minimum != null && build < minimum,
      available: latest != null && build < latest,
      url: json['update_url'] as String?,
      message: json['message'] as String?,
      latest: latest,
    );
  }

  final bool required;
  final bool available;
  final String? url;
  final String? message;
  final int? latest;

  /// The banner was closed. It comes back for a newer build, not this one.
  final bool dismissed;

  bool get showBanner => available && !required && !dismissed;

  AppUpdate dismiss() => AppUpdate(
        required: required,
        available: available,
        url: url,
        message: message,
        latest: latest,
        dismissed: true,
      );
}

/// This build's number, from `--dart-define=APP_BUILD=…`. Zero in development
/// builds, which never check. A provider so tests can be any build.
final appBuildProvider = Provider<int>((ref) => AppConfig.build);

final appUpdateProvider = NotifierProvider<AppUpdateController, AppUpdate?>(AppUpdateController.new);

class AppUpdateController extends Notifier<AppUpdate?> {
  static const _every = Duration(hours: 1);
  DateTime? _checkedAt;

  @override
  AppUpdate? build() => null;

  /// Asks the server, at most once an hour. Failures leave things as they are:
  /// nobody is blocked because the check couldn't reach the server.
  Future<void> check({DateTime? now}) async {
    final build = ref.read(appBuildProvider);
    if (build <= 0) return;
    final at = now ?? DateTime.now();
    if (_checkedAt != null && at.difference(_checkedAt!) < _every) return;
    _checkedAt = at;
    try {
      final data = await ref.read(apiClientProvider).get('/app/version', query: {
        'platform': defaultTargetPlatform == TargetPlatform.iOS ? 'ios' : 'android',
      });
      if (!ref.mounted) return;
      final next = AppUpdate.decide(Map<String, dynamic>.from(data as Map), build: build);
      // A banner closed for this build stays closed.
      final before = state;
      state = before != null && before.dismissed && before.latest == next.latest && !next.required
          ? next.dismiss()
          : next;
    } catch (_) {
      _checkedAt = null;
    }
  }

  void dismiss() {
    final current = state;
    if (current != null) state = current.dismiss();
  }
}

Future<void> _openUpdate(BuildContext context, String? url) async {
  final uri = url == null ? null : Uri.tryParse(url);
  final opened = uri != null && await launchUrl(uri, mode: LaunchMode.externalApplication);
  if (!opened && context.mounted) {
    ScaffoldMessenger.maybeOf(context)
        ?.showSnackBar(const SnackBar(content: Text("Couldn't open the download. Try again later.")));
  }
}

/// In front of everything when this build is too old to go on.
class UpdateRequiredScreen extends StatelessWidget {
  const UpdateRequiredScreen({super.key, required this.update});

  final AppUpdate update;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Center(child: RadiusMark(size: 64)),
              const SizedBox(height: 24),
              Text('Update required', textAlign: TextAlign.center, style: text.headlineSmall),
              const SizedBox(height: 10),
              Text(
                update.message ??
                    'This version of Hoodora is no longer supported. Get the new one to carry on; '
                        'your account and everything in it stay as they are.',
                textAlign: TextAlign.center,
                style: text.bodyLarge?.copyWith(color: Palette.muted),
              ),
              const SizedBox(height: 28),
              if (update.url != null)
                FilledButton(onPressed: () => _openUpdate(context, update.url), child: const Text('Update now')),
            ],
          ),
        ),
      ),
    );
  }
}

/// A thin strip above the app: a newer build is out. Closing it hides it until
/// the next one.
class UpdateBanner extends ConsumerWidget {
  const UpdateBanner({super.key, required this.update});

  final AppUpdate update;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final text = Theme.of(context).textTheme;
    return Material(
      color: Palette.glowTint,
      child: SafeArea(
        bottom: false,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(16, 6, 4, 6),
          child: Row(
            children: [
              Icon(Icons.system_update_rounded, size: 20, color: Palette.primaryDark),
              const SizedBox(width: 10),
              Expanded(
                child: Text(
                  update.message ?? 'A new version of Hoodora is available.',
                  style: text.bodyMedium?.copyWith(color: Palette.primaryDark),
                ),
              ),
              if (update.url != null)
                TextButton(onPressed: () => _openUpdate(context, update.url), child: const Text('Update')),
              // No tooltip: the banner sits above the navigator, where there is no
              // overlay to show one in.
              IconButton(
                onPressed: () => ref.read(appUpdateProvider.notifier).dismiss(),
                icon: Icon(Icons.close_rounded, size: 18, color: Palette.primaryDark, semanticLabel: 'Later'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
