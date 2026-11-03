import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/auth/auth_repository.dart';
import '../features/chats/chat_repository.dart';
import '../features/feed/feed_repository.dart';
import '../features/guest/guest_repository.dart';
import '../features/market/listing_repository.dart';
import '../features/me/account_repository.dart';
import '../features/profile/profile_repository.dart';
import '../features/reviews/review_repository.dart';
import '../features/services/service_repository.dart';
import 'api/api_client.dart';
import 'api/media_uploader.dart';
import 'api/token_store.dart';
import 'location.dart';
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

final serviceRepositoryProvider = Provider<ServiceRepository>(
  (ref) => ServiceRepository(ref.watch(apiClientProvider)),
);

final listingRepositoryProvider = Provider<ListingRepository>(
  (ref) => ListingRepository(ref.watch(apiClientProvider)),
);

final accountRepositoryProvider = Provider<AccountRepository>(
  (ref) => AccountRepository(ref.watch(apiClientProvider)),
);

final profileRepositoryProvider = Provider<ProfileRepository>(
  (ref) => ProfileRepository(ref.watch(apiClientProvider)),
);

final reviewRepositoryProvider = Provider<ReviewRepository>(
  (ref) => ReviewRepository(ref.watch(apiClientProvider)),
);

final guestRepositoryProvider = Provider<GuestRepository>(
  (ref) => GuestRepository(ref.watch(apiClientProvider)),
);

/// Where the phone is. A provider so tests can stand somewhere without GPS.
final locationReaderProvider = Provider<LocationReader>((ref) => const LocationReader());

final mediaUploaderProvider = Provider<MediaUploader>(
  (ref) => MediaUploader(ref.watch(apiClientProvider)),
);

/// The realtime socket. Started and stopped by the signed-in shell.
final realtimeProvider = Provider<RealtimeClient>((ref) {
  final client = RealtimeClient(ref.watch(apiClientProvider));
  ref.onDispose(client.dispose);
  return client;
});
