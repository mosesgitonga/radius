import '../../core/api/api_client.dart';
import '../../core/offline_store.dart';
import '../../core/location.dart';
import 'auth_models.dart';

/// The account endpoints: codes, sign-up, the home area, sign-out.
class AuthRepository {
  AuthRepository(this._api, [OfflineStore? offline]) : _offline = offline ?? MemoryOfflineStore();

  final ApiClient _api;
  final OfflineStore _offline;

  Future<OtpChallenge> requestCode(String phoneNumber) async {
    final data = await _api.post('/auth/otp/request',
        body: {'phone_number': phoneNumber}, auth: false);
    return OtpChallenge.fromJson(Map<String, dynamic>.from(data as Map));
  }

  /// Checks a code. An existing account is signed in here; a new number gets a
  /// single-use token for [signUp].
  Future<VerifyOutcome> verifyCode(String phoneNumber, String code) async {
    final data = Map<String, dynamic>.from(await _api.post(
      '/auth/otp/verify',
      body: {'phone_number': phoneNumber, 'code': code, ...await _api.deviceFields()},
      auth: false,
    ) as Map);
    if (data['status'] == 'signed_in') {
      await _api.startSession(TokenPair.fromJson(Map<String, dynamic>.from(data['tokens'] as Map)));
      return const SignedIn();
    }
    return SignupRequired(data['signup_token'] as String);
  }

  /// Creates the account. The three consents are separate, as the Data
  /// Protection Act requires; marketing is optional and off unless chosen.
  Future<void> signUp({
    required String signupToken,
    required String displayName,
    required bool acceptMarketing,
  }) async {
    final data = Map<String, dynamic>.from(await _api.post(
      '/auth/signup',
      body: {
        'signup_token': signupToken,
        'display_name': displayName,
        'is_adult': true,
        'accept_terms': true,
        'accept_privacy_policy': true,
        'accept_location_use': true,
        'accept_marketing': acceptMarketing,
        ...await _api.deviceFields(),
      },
      auth: false,
    ) as Map);
    await _api.startSession(TokenPair.fromJson(Map<String, dynamic>.from(data['tokens'] as Map)));
  }

  Future<Me> me() async =>
      Me.fromJson(Map<String, dynamic>.from(await fetchOrRecall(_offline, 'me', () => _api.get('/me')) as Map));

  /// The account as last saved, for opening the app offline.
  Future<Me?> savedMe() async {
    final saved = await _offline.read('me');
    return saved is Map ? Me.fromJson(Map<String, dynamic>.from(saved)) : null;
  }

  /// Signing out, or the session ending: nothing saved may outlive it.
  Future<void> forgetSaved() => _offline.wipe();

  /// Sends a GPS reading once; the server keeps only its area's cell and answers
  /// with the area's name. Nothing is stored on the phone.
  ///
  /// With [atHome] false the person is signing up away from home: the home is
  /// provisional until they set the real one (decision M.10).
  Future<AreaLabel> setHome(Reading reading, {bool atHome = true}) async {
    final data = Map<String, dynamic>.from(
      await _api.put('/me/location', body: {...reading.toJson(), 'at_home': atHome}) as Map,
    );
    return AreaLabel.fromJson(Map<String, dynamic>.from(data['area_label'] as Map));
  }

  /// The daily check: tells the server whether the phone is at home today. Never
  /// moves home.
  Future<void> checkPresence(Reading reading) => _api.post('/me/presence', body: reading.toJson());

  Future<bool> resume() => _api.resume();

  Future<void> signOut(String? refreshToken) async {
    try {
      if (refreshToken != null) {
        await _api.post('/auth/logout', body: {'refresh_token': refreshToken}, auth: false);
      }
    } finally {
      // Signed out on this phone whatever the server said: a sign-out must
      // never fail for the person holding the phone.
      await _api.endSession();
    }
  }
}
