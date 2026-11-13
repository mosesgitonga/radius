import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'core/app_update.dart';
import 'core/appearance.dart';
import 'core/router.dart';
import 'core/theme.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    // Reads the config the Gradle plugin builds from google-services.json. A build
    // without it still runs, just without push notifications.
    await Firebase.initializeApp();
  } catch (error) {
    debugPrint('firebase_unavailable: $error');
  }
  runApp(const ProviderScope(child: MyRadiusApp()));
}

class MyRadiusApp extends ConsumerStatefulWidget {
  const MyRadiusApp({super.key});

  @override
  ConsumerState<MyRadiusApp> createState() => _MyRadiusAppState();
}

class _MyRadiusAppState extends ConsumerState<MyRadiusApp> {
  late final AppLifecycleListener _lifecycle;

  @override
  void initState() {
    super.initState();
    // Whether this build should update: at launch, and on coming back to the
    // app. Code patches need none of this; they apply on the next launch.
    Future.microtask(() => ref.read(appUpdateProvider.notifier).check());
    _lifecycle = AppLifecycleListener(onResume: () => ref.read(appUpdateProvider.notifier).check());
  }

  @override
  void dispose() {
    _lifecycle.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return MaterialApp.router(
      title: 'Hoodora',
      theme: buildTheme(),
      darkTheme: buildTheme(Brightness.dark),
      themeMode: ref.watch(themeModeProvider),
      routerConfig: ref.watch(routerProvider),
      debugShowCheckedModeBanner: false,
      builder: (context, child) {
        // Screens read colours from `Palette` directly, so it must match the theme
        // before they build; switching modes rebuilds them all, under a new key.
        final brightness = Theme.of(context).brightness;
        Palette.use(brightness);
        final update = ref.watch(appUpdateProvider);
        if (update != null && update.required) {
          return KeyedSubtree(key: ValueKey(brightness), child: UpdateRequiredScreen(update: update));
        }
        final app = KeyedSubtree(key: ValueKey(brightness), child: child ?? const SizedBox.shrink());
        if (update == null || !update.showBanner) return app;
        return Column(
          children: [
            UpdateBanner(update: update),
            // The banner already sits under the status bar.
            Expanded(child: MediaQuery.removePadding(context: context, removeTop: true, child: app)),
          ],
        );
      },
    );
  }
}
