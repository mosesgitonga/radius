DateTime? _date(Object? value) => value == null ? null : DateTime.parse(value as String);

/// Where the account stands with the moderators.
class Standing {
  const Standing({
    required this.status,
    required this.restricted,
    required this.isVerifiedNeighbour,
    required this.vouchesReceived,
    required this.warnings,
    this.suspendedUntil,
    this.restrictedUntil,
    this.restrictionReason,
  });

  factory Standing.fromJson(Map<String, dynamic> json) => Standing(
        status: json['status'] as String,
        suspendedUntil: _date(json['suspended_until']),
        restricted: json['restricted'] as bool? ?? false,
        restrictedUntil: _date(json['restricted_until']),
        restrictionReason: json['restriction_reason'] as String?,
        isVerifiedNeighbour: json['is_verified_neighbour'] as bool? ?? false,
        vouchesReceived: json['vouches_received'] as int? ?? 0,
        warnings: [
          for (final w in (json['warnings'] as List?) ?? const [])
            Warning(reason: (w as Map)['reason'] as String, at: DateTime.parse(w['created_at'] as String)),
        ],
      );

  final String status;
  final DateTime? suspendedUntil;
  final bool restricted;
  final DateTime? restrictedUntil;
  final String? restrictionReason;
  final bool isVerifiedNeighbour;
  final int vouchesReceived;
  final List<Warning> warnings;

  bool get suspended => status == 'suspended';
}

class Warning {
  const Warning({required this.reason, required this.at});
  final String reason;
  final DateTime at;
}

/// One consent decision, with the version of the document agreed to.
class Consent {
  const Consent({required this.type, required this.granted, required this.version, required this.recordedAt});

  factory Consent.fromJson(Map<String, dynamic> json) => Consent(
        type: json['consent_type'] as String,
        granted: json['granted'] as bool,
        version: json['document_version'] as String,
        recordedAt: DateTime.parse(json['recorded_at'] as String),
      );

  final String type;
  final bool granted;
  final String version;
  final DateTime recordedAt;

  /// Only marketing can be switched; the rest go with the account.
  bool get optional => type == 'marketing';
}

const consentNames = <String, String>{
  'terms': 'Terms of use',
  'privacy_policy': 'Privacy policy',
  'location_use': 'Using my home area to show what is nearby',
  'marketing': 'News and offers from Hoodora',
};

class BlockedPerson {
  const BlockedPerson({required this.userId, required this.displayName, required this.blockedAt});

  factory BlockedPerson.fromJson(Map<String, dynamic> json) => BlockedPerson(
        userId: json['user_id'] as String,
        displayName: json['display_name'] as String,
        blockedAt: DateTime.parse(json['blocked_at'] as String),
      );

  final String userId;
  final String displayName;
  final DateTime blockedAt;
}

/// One signed-in device.
class DeviceSession {
  const DeviceSession({
    required this.id,
    required this.createdAt,
    required this.lastUsedAt,
    required this.isCurrent,
    this.name,
    this.platform,
  });

  factory DeviceSession.fromJson(Map<String, dynamic> json) => DeviceSession(
        id: json['id'] as String,
        name: json['device_name'] as String?,
        platform: json['device_platform'] as String?,
        createdAt: DateTime.parse(json['created_at'] as String),
        lastUsedAt: DateTime.parse(json['last_used_at'] as String),
        isCurrent: json['is_current'] as bool? ?? false,
      );

  final String id;
  final String? name;
  final String? platform;
  final DateTime createdAt;
  final DateTime lastUsedAt;
  final bool isCurrent;

  String get label =>
      name ??
      switch (platform) {
        'android' => 'Android phone',
        'ios' => 'iPhone',
        _ => 'A device',
      };
}

class NotificationPreference {
  const NotificationPreference({
    required this.type,
    required this.push,
    required this.inApp,
    required this.preview,
  });

  factory NotificationPreference.fromJson(Map<String, dynamic> json) => NotificationPreference(
        type: json['type'] as String,
        push: json['push_enabled'] as bool,
        inApp: json['in_app_enabled'] as bool,
        preview: json['show_preview'] as bool? ?? false,
      );

  final String type;
  final bool push;
  final bool inApp;
  final bool preview;

  NotificationPreference copyWith({bool? push, bool? inApp, bool? preview}) => NotificationPreference(
        type: type,
        push: push ?? this.push,
        inApp: inApp ?? this.inApp,
        preview: preview ?? this.preview,
      );
}

/// What each kind of notification is, in words.
const notificationNames = <String, String>{
  'message': 'Messages',
  'message_request': 'Message requests',
  'chat_invite': 'Group chat invitations',
  'comment': 'Comments on my posts',
  'reply': 'Replies to my comments',
  'reaction': '"Helpful" on my posts',
  'group_invite': 'Group invitations',
  'group_join_request': 'Requests to join my groups',
  'group_join_approved': 'Joining a group',
  'group_post_approved': 'My group posts approved',
  'listing_inquiry': 'Questions about my listings',
  'service_inquiry': 'Questions about my services',
  'review_received': 'Reviews of me',
  'review_reply': 'Replies to my reviews',
  'review_prompt': 'Reminders to leave a review',
  'listing_expiring': 'My listings about to expire',
  'event_invite': 'Event invitations',
  'event_rsvp_request': 'Requests to come to my events',
  'event_rsvp_approved': 'My place at an event confirmed',
  'event_reminder': 'Event reminders',
  'event_updated': 'Changes to events I am going to',
  'event_cancelled': 'Events cancelled',
  'circle': 'Circle requests',
  'account_notice': 'Notices about my account',
};

String notificationName(String type) {
  final known = notificationNames[type];
  if (known != null) return known;
  final words = type.replaceAll('_', ' ');
  return words.isEmpty ? type : words[0].toUpperCase() + words.substring(1);
}

/// When the account will be purged.
class DeletionStatus {
  const DeletionStatus({required this.requestedAt, required this.purgeAfter});

  factory DeletionStatus.fromJson(Map<String, dynamic> json) => DeletionStatus(
        requestedAt: DateTime.parse(json['requested_at'] as String),
        purgeAfter: DateTime.parse(json['purge_after'] as String),
      );

  final DateTime requestedAt;
  final DateTime purgeAfter;
}
