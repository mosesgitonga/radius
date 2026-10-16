import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../features/auth/auth_repository.dart';
import 'api/api_client.dart';
import 'api/token_store.dart';

/// The app's shared services. Tests override [tokenStoreProvider] and
/// [apiClientProvider] with in-memory versions.
final tokenStoreProvider = Provider<TokenStore>((ref) => SecureTokenStore());

final apiClientProvider = Provider<ApiClient>(
  (ref) => ApiClient(store: ref.watch(tokenStoreProvider)),
);

final authRepositoryProvider = Provider<AuthRepository>(
  (ref) => AuthRepository(ref.watch(apiClientProvider)),
);
