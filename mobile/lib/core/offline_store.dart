import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'dart:typed_data';

import 'package:cryptography/cryptography.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:path_provider/path_provider.dart';

import 'api/api_exception.dart';

/// What the app keeps on the phone so it can open offline (decision M.11): the
/// account, the Home feed's latest posts, the recent chats and their latest
/// messages. Each is an API response saved as it arrived.
///
/// Encrypted with AES-GCM under a key kept in the phone's secure keystore, so the
/// files alone are unreadable. Everything, key included, is wiped when the person
/// signs out or the session ends.
abstract class OfflineStore {
  Future<Object?> read(String name);
  Future<void> write(String name, Object? json);

  /// Deletes every saved file whose name starts with [prefix] and is not in [keep].
  Future<void> retain(String prefix, Set<String> keep);
  Future<void> wipe();
}

/// The newest copy from the server, saved on the way through; or, when the phone
/// can't reach the server, the copy saved last time. [keep] trims what is saved.
Future<dynamic> fetchOrRecall(
  OfflineStore store,
  String name,
  Future<dynamic> Function() fetch, {
  Object? Function(dynamic json)? keep,
}) async {
  try {
    final fresh = await fetch();
    // Saved in the background: the screen shows the fresh copy without waiting.
    unawaited(store.write(name, keep == null ? fresh : keep(fresh)));
    return fresh;
  } on ApiException catch (error) {
    if (!error.isNetwork) rethrow;
    final saved = await store.read(name);
    if (saved == null) rethrow;
    return saved;
  }
}

class EncryptedOfflineStore implements OfflineStore {
  EncryptedOfflineStore([FlutterSecureStorage? keystore]) : _keystore = keystore ?? const FlutterSecureStorage();

  static const _keyName = 'offline_cache_key_v1';
  final FlutterSecureStorage _keystore;
  final _cipher = AesGcm.with256bits();
  SecretKey? _key;
  Directory? _folder;

  Future<Directory> _dir() async {
    final folder = _folder ??= Directory('${(await getApplicationSupportDirectory()).path}/offline');
    if (!await folder.exists()) await folder.create(recursive: true);
    return folder;
  }

  Future<SecretKey> _secret() async {
    final known = _key;
    if (known != null) return known;
    final stored = await _keystore.read(key: _keyName);
    if (stored != null) return _key = SecretKey(base64Decode(stored));
    final random = Random.secure();
    final bytes = List<int>.generate(32, (_) => random.nextInt(256));
    await _keystore.write(key: _keyName, value: base64Encode(bytes));
    return _key = SecretKey(bytes);
  }

  static String _fileName(String name) => '${name.replaceAll(RegExp(r'[^A-Za-z0-9_.-]'), '_')}.bin';

  @override
  Future<Object?> read(String name) async {
    try {
      final file = File('${(await _dir()).path}/${_fileName(name)}');
      if (!await file.exists()) return null;
      final box = SecretBox.fromConcatenation(
        await file.readAsBytes(),
        nonceLength: _cipher.nonceLength,
        macLength: _cipher.macAlgorithm.macLength,
      );
      final clear = await _cipher.decrypt(box, secretKey: await _secret());
      return jsonDecode(utf8.decode(clear));
    } catch (_) {
      // Missing, from an older key, or damaged: as good as not saved.
      return null;
    }
  }

  @override
  Future<void> write(String name, Object? json) async {
    try {
      final box = await _cipher.encrypt(utf8.encode(jsonEncode(json)), secretKey: await _secret());
      final file = File('${(await _dir()).path}/${_fileName(name)}');
      final temp = File('${file.path}.tmp');
      await temp.writeAsBytes(Uint8List.fromList(box.concatenation()), flush: true);
      await temp.rename(file.path);
    } catch (_) {
      // Not saved this time; the app works on without it.
    }
  }

  @override
  Future<void> retain(String prefix, Set<String> keep) async {
    try {
      final kept = {for (final name in keep) _fileName(name)};
      final filePrefix = _fileName(prefix).replaceAll('.bin', '');
      await for (final entry in (await _dir()).list()) {
        final base = entry.uri.pathSegments.last;
        if (base.startsWith(filePrefix) && !kept.contains(base)) await entry.delete();
      }
    } catch (_) {
      // Pruned next time.
    }
  }

  @override
  Future<void> wipe() async {
    try {
      final folder = await _dir();
      if (await folder.exists()) await folder.delete(recursive: true);
      _folder = null;
    } catch (_) {
      // Nothing to wipe, or the folder is gone already.
    }
    try {
      await _keystore.delete(key: _keyName);
    } catch (_) {
      // Without the key the files could not be read anyway.
    }
    _key = null;
  }
}

/// For tests: the same behaviour, in memory.
class MemoryOfflineStore implements OfflineStore {
  final Map<String, String> saved = {};

  @override
  Future<Object?> read(String name) async => saved.containsKey(name) ? jsonDecode(saved[name]!) : null;

  @override
  Future<void> write(String name, Object? json) async => saved[name] = jsonEncode(json);

  @override
  Future<void> retain(String prefix, Set<String> keep) async =>
      saved.removeWhere((name, _) => name.startsWith(prefix) && !keep.contains(name));

  @override
  Future<void> wipe() async => saved.clear();
}

final offlineStoreProvider = Provider<OfflineStore>((ref) => EncryptedOfflineStore());

/// Whether the server answered the last request. False while offline, when the
/// app is showing what it saved.
final onlineProvider = NotifierProvider<OnlineController, bool>(OnlineController.new);

class OnlineController extends Notifier<bool> {
  @override
  bool build() => true;

  void set(bool online) {
    if (state != online) state = online;
  }
}
