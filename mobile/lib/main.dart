import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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

class MyRadiusApp extends ConsumerWidget {
  const MyRadiusApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    return MaterialApp.router(
      title: 'MyRadius',
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
        return KeyedSubtree(key: ValueKey(brightness), child: child ?? const SizedBox.shrink());
      },
    );
  }
}
