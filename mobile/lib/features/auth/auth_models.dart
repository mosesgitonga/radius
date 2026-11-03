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
    this.phoneNumber,
    this.memberSince,
    this.ratingAverage,
    this.ratingCount = 0,
    this.deletionRequestedAt,
    this.nextHomeChangeAllowedAt,
    this.bio,
    this.avatar = const {},
    this.cover = const {},
    this.canContactStrangers = true,
    this.presenceDaysThisWeek = 0,
    this.contactUnlockDays = 4,
    this.contactUnlockWindowDays = 7,
    this.presenceCheckDue = false,
    this.showInNearby = false,
    this.homeProvisional = false,
    this.homeConfirmBy,
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
        phoneNumber: json['phone_number'] as String?,
        memberSince: _date(json['member_since']),
        ratingAverage: (json['rating_average'] as num?)?.toDouble(),
        ratingCount: json['rating_count'] as int? ?? 0,
        deletionRequestedAt: _date(json['deletion_requested_at']),
        nextHomeChangeAllowedAt: _date(json['next_home_change_allowed_at']),
        bio: json['bio'] as String?,
        avatar: _sizes(json['avatar']),
        cover: _sizes(json['cover']),
        canContactStrangers: json['can_contact_strangers'] as bool? ?? true,
        presenceDaysThisWeek: json['presence_days_this_week'] as int? ?? 0,
        contactUnlockDays: json['contact_unlock_days'] as int? ?? 4,
        contactUnlockWindowDays: json['contact_unlock_window_days'] as int? ?? 7,
        presenceCheckDue: json['presence_check_due'] as bool? ?? false,
        showInNearby: json['show_in_nearby'] as bool? ?? false,
        homeProvisional: json['home_provisional'] as bool? ?? false,
        homeConfirmBy: _date(json['home_confirm_by']),
      );

  static DateTime? _date(Object? value) => value == null ? null : DateTime.parse(value as String);

  static Map<int, String> _sizes(Object? json) => json is Map
      ? {for (final entry in json.entries) int.parse('${entry.key}'): (entry.value as Map)['url'] as String}
      : const {};

  final String id;
  final String displayName;
  final String status;
  final bool hasHomeLocation;
  final bool isVerifiedNeighbour;
  final AreaLabel? areaLabel;
  final String? phoneNumber;
  final DateTime? memberSince;
  final double? ratingAverage;
  final int ratingCount;

  /// Set while the account is waiting out its deletion grace period.
  final DateTime? deletionRequestedAt;

  /// Null when the home area may change now.
  final DateTime? nextHomeChangeAllowedAt;

  final String? bio;

  /// Photo and cover sizes, keyed by width; empty when there is none.
  final Map<int, String> avatar;
  final Map<int, String> cover;

  /// Whether I may start conversations with people I haven't talked to: after
  /// [contactUnlockDays] of the last [contactUnlockWindowDays] days at home, or
  /// with the badge (decision M.7).
  final bool canContactStrangers;
  final int presenceDaysThisWeek;
  final int contactUnlockDays;
  final int contactUnlockWindowDays;

  /// True when the app hasn't yet checked today whether the phone is at home.
  final bool presenceCheckDue;

  /// Whether I chose to be listed in People nearby (decision M.8).
  final bool showInNearby;

  /// Home was set away from home at sign-up; set the real one by [homeConfirmBy]
  /// (decision M.10).
  final bool homeProvisional;
  final DateTime? homeConfirmBy;

  bool get deletionPending => deletionRequestedAt != null;
}
