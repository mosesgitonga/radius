/// What the server says after sending a code.
class OtpChallenge {
  const OtpChallenge({
    required this.expiresInSeconds,
    required this.resendAfterSeconds,
    required this.maskedNumber,
  });

  factory OtpChallenge.fromJson(Map<String, dynamic> json) => OtpChallenge(
        expiresInSeconds: json['expires_in_seconds'] as int,
        resendAfterSeconds: json['resend_after_seconds'] as int,
        maskedNumber: json['phone_number_masked'] as String,
      );

  final int expiresInSeconds;
  final int resendAfterSeconds;
  final String maskedNumber;
}

/// A correct code either signs an existing account in, or clears a new number
/// to sign up.
sealed class VerifyOutcome {
  const VerifyOutcome();
}

class SignedIn extends VerifyOutcome {
  const SignedIn();
}

class SignupRequired extends VerifyOutcome {
  const SignupRequired(this.signupToken);
  final String signupToken;
}

/// How a place is named: an area label, never a position.
class AreaLabel {
  const AreaLabel({required this.label, required this.precision});

  factory AreaLabel.fromJson(Map<String, dynamic> json) => AreaLabel(
        label: json['label'] as String,
        precision: json['precision'] as String,
      );

  final String label;
  final String precision;
}

/// The signed-in account, as `GET /v1/me` returns it.
class Me {
  const Me({
    required this.id,
    required this.displayName,
    required this.status,
    required this.hasHomeLocation,
    required this.isVerifiedNeighbour,
    this.areaLabel,
  });

  factory Me.fromJson(Map<String, dynamic> json) => Me(
        id: json['id'] as String,
        displayName: json['display_name'] as String,
        status: json['status'] as String,
        hasHomeLocation: json['has_home_location'] as bool,
        isVerifiedNeighbour: json['is_verified_neighbour'] as bool,
        areaLabel: json['area_label'] == null
            ? null
            : AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
      );

  final String id;
  final String displayName;
  final String status;
  final bool hasHomeLocation;
  final bool isVerifiedNeighbour;
  final AreaLabel? areaLabel;
}
