import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/push.dart';
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
      // The session is over: nothing saved for it may stay on the phone.
      ref.read(authRepositoryProvider).forgetSaved();
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
      state = error.isNetwork ? await _openOffline() : const AuthSignedOut();
    }
  }

  /// No connection at launch, but a session is stored: open on what was saved
  /// (decision M.11). The first request once the phone is back online renews the
  /// session. With nothing saved, the offline screen asks to try again.
  Future<AuthState> _openOffline() async {
    final hasSession = await ref.read(tokenStoreProvider).readRefreshToken() != null;
    final saved = hasSession ? await ref.read(authRepositoryProvider).savedMe() : null;
    if (saved == null) return const AuthOffline();
    return saved.hasHomeLocation ? AuthSignedIn(saved) : AuthNeedsHome(saved);
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

  /// The account changed (renamed, deletion cancelled): show the new one.
  void replaceMe(Me me) {
    if (state is AuthSignedIn) state = AuthSignedIn(me);
  }

  /// The server has already ended every session (a deletion request does):
  /// forget this one here too, without asking it again.
  Future<void> endedByServer() async {
    await ref.read(authRepositoryProvider).forgetSaved();
    await ref.read(apiClientProvider).endSession();
    state = const AuthSignedOut();
  }

  Future<void> signOut() async {
    // While the session still works: stop pushes to this phone first.
    await ref.read(pushServiceProvider).stop();
    final refreshToken = await ref.read(tokenStoreProvider).readRefreshToken();
    try {
      await ref.read(authRepositoryProvider).signOut(refreshToken);
    } on ApiException {
      // Already signed out locally; the server session expires on its own.
    }
    await ref.read(authRepositoryProvider).forgetSaved();
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
