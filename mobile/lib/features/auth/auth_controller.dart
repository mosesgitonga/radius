import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import 'auth_models.dart';

/// Where the person is in the app's lifecycle. The router follows this.
sealed class AuthState {
  const AuthState();
}

/// At launch, while a stored session is being restored.
class AuthRestoring extends AuthState {
  const AuthRestoring();
}

class AuthSignedOut extends AuthState {
  const AuthSignedOut();
}

/// A session is stored but the server can't be reached to check it.
class AuthOffline extends AuthState {
  const AuthOffline();
}

/// Signed in, but no home area yet: nothing nearby can be shown without one.
class AuthNeedsHome extends AuthState {
  const AuthNeedsHome(this.me);
  final Me me;
}

class AuthSignedIn extends AuthState {
  const AuthSignedIn(this.me);
  final Me me;
}

final authControllerProvider =
    NotifierProvider<AuthController, AuthState>(AuthController.new);

class AuthController extends Notifier<AuthState> {
  @override
  AuthState build() {
    ref.read(apiClientProvider).onSessionExpired = () {
      state = const AuthSignedOut();
    };
    Future.microtask(_restore);
    return const AuthRestoring();
  }

  Future<void> _restore() async {
    final repo = ref.read(authRepositoryProvider);
    try {
      if (await repo.resume()) {
        await _loadMe();
      } else {
        state = const AuthSignedOut();
      }
    } on ApiException catch (error) {
      state = error.isNetwork ? const AuthOffline() : const AuthSignedOut();
    }
  }

  Future<void> retry() async {
    state = const AuthRestoring();
    await _restore();
  }

  /// After a code or a sign-up has started a session.
  Future<void> sessionStarted() => _loadMe();

  /// After the home area is set.
  Future<void> refreshMe() => _loadMe();

  Future<void> _loadMe() async {
    final me = await ref.read(authRepositoryProvider).me();
    state = me.hasHomeLocation ? AuthSignedIn(me) : AuthNeedsHome(me);
  }

  Future<void> signOut() async {
    final refreshToken = await ref.read(tokenStoreProvider).readRefreshToken();
    try {
      await ref.read(authRepositoryProvider).signOut(refreshToken);
    } on ApiException {
      // Already signed out locally; the server session expires on its own.
    }
    state = const AuthSignedOut();
  }
}

/// The sign-in flow's in-between values: the number, the code's challenge, and
/// the sign-up token for a new number. Cleared when the flow ends.
class SignInFlow {
  const SignInFlow({this.phoneNumber, this.challenge, this.signupToken});

  final String? phoneNumber;
  final OtpChallenge? challenge;
  final String? signupToken;
}

final signInFlowProvider =
    NotifierProvider<SignInFlowController, SignInFlow>(SignInFlowController.new);

class SignInFlowController extends Notifier<SignInFlow> {
  @override
  SignInFlow build() => const SignInFlow();

  void codeSent(String phoneNumber, OtpChallenge challenge) =>
      state = SignInFlow(phoneNumber: phoneNumber, challenge: challenge);

  void signupRequired(String token) => state = SignInFlow(
        phoneNumber: state.phoneNumber,
        challenge: state.challenge,
        signupToken: token,
      );

  void reset() => state = const SignInFlow();
}
