import '../../core/api/api_client.dart';
import 'auth_models.dart';

/// The account endpoints: codes, sign-up, the home area, sign-out.
class AuthRepository {
  AuthRepository(this._api);

  final ApiClient _api;

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
      Me.fromJson(Map<String, dynamic>.from(await _api.get('/me') as Map));

  /// Sends a position once; the server keeps only its area's cell and answers
  /// with the area's name. Nothing is stored on the phone.
  Future<AreaLabel> setHome({
    required double latitude,
    required double longitude,
    double? accuracyMetres,
  }) async {
    final data = Map<String, dynamic>.from(await _api.put('/me/location', body: {
      'latitude': latitude,
      'longitude': longitude,
      'accuracy_metres': ?accuracyMetres,
    }) as Map);
    return AreaLabel.fromJson(Map<String, dynamic>.from(data['area_label'] as Map));
  }

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
