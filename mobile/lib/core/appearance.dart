import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:path_provider/path_provider.dart';

/// Where the light/dark choice is kept: a tiny file in the app's private folder.
/// A display preference, not personal data, so it outlives signing out.
class AppearanceStore {
  const AppearanceStore();

  Future<File> _file() async => File('${(await getApplicationSupportDirectory()).path}/appearance.json');

  Future<ThemeMode> load() async {
    try {
      final file = await _file();
      if (!await file.exists()) return ThemeMode.system;
      final name = (jsonDecode(await file.readAsString()) as Map)['theme'] as String?;
      return ThemeMode.values.firstWhere((mode) => mode.name == name, orElse: () => ThemeMode.system);
    } catch (_) {
      return ThemeMode.system;
    }
  }

  Future<void> save(ThemeMode mode) async {
    try {
      await (await _file()).writeAsString(jsonEncode({'theme': mode.name}));
    } catch (_) {
      // Not saved: the choice still holds until the app closes.
    }
  }
}

final appearanceStoreProvider = Provider<AppearanceStore>((ref) => const AppearanceStore());

/// System, light or dark. Follows the phone until the person picks otherwise.
final themeModeProvider = NotifierProvider<ThemeModeController, ThemeMode>(ThemeModeController.new);

class ThemeModeController extends Notifier<ThemeMode> {
  @override
  ThemeMode build() {
    Future.microtask(() async {
      final saved = await ref.read(appearanceStoreProvider).load();
      if (saved != state) state = saved;
    });
    return ThemeMode.system;
  }

  Future<void> choose(ThemeMode mode) async {
    state = mode;
    await ref.read(appearanceStoreProvider).save(mode);
  }
}
