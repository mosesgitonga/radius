import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/auth/auth_repository.dart';
import '../features/chats/chat_repository.dart';
import '../features/feed/feed_repository.dart';
import 'api/api_client.dart';
import 'api/media_uploader.dart';
import 'api/token_store.dart';
import 'realtime.dart';

/// The app's shared services. Tests override [tokenStoreProvider],
/// [apiClientProvider] and [realtimeProvider] with in-memory versions.
final tokenStoreProvider = Provider<TokenStore>((ref) => SecureTokenStore());

final apiClientProvider = Provider<ApiClient>(
  (ref) => ApiClient(store: ref.watch(tokenStoreProvider)),
);

final authRepositoryProvider = Provider<AuthRepository>(
  (ref) => AuthRepository(ref.watch(apiClientProvider)),
);

final feedRepositoryProvider = Provider<FeedRepository>(
  (ref) => FeedRepository(ref.watch(apiClientProvider)),
);

final chatRepositoryProvider = Provider<ChatRepository>(
  (ref) => ChatRepository(ref.watch(apiClientProvider)),
);

final mediaUploaderProvider = Provider<MediaUploader>(
  (ref) => MediaUploader(ref.watch(apiClientProvider)),
);

/// The realtime socket. Started and stopped by the signed-in shell.
final realtimeProvider = Provider<RealtimeClient>((ref) {
  final client = RealtimeClient(ref.watch(apiClientProvider));
  ref.onDispose(client.dispose);
  return client;
});
