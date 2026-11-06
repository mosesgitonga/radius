import 'package:firebase_core/firebase_core.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

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
      routerConfig: ref.watch(routerProvider),
      debugShowCheckedModeBanner: false,
    );
  }
}
