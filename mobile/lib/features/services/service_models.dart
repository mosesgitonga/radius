import 'package:intl/intl.dart';

import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// The three kinds of service post, in the order the tabs show them.
enum ServiceKind {
  offering('offering', 'Offering'),
  seeking('seeking', 'Looking for'),
  hiring('hiring', 'Jobs');

  const ServiceKind(this.key, this.label);
  final String key;
  final String label;

  static ServiceKind parse(String key) => values.firstWhere((k) => k.key == key);
}

/// Labels for the server's seeded categories. The list is configurable on the
/// server; a key the app does not know is shown as plain words.
const serviceCategories = <String, String>{
  'plumbing': 'Plumbing',
  'electrical': 'Electrical',
  'laundry': 'Laundry',
  'house_help': 'House help',
  'tutoring': 'Tutoring',
  'delivery': 'Delivery',
  'mechanics': 'Mechanics',
  'salon_barber': 'Salon & barber',
  'tailoring': 'Tailoring',
  'construction': 'Construction',
  'catering': 'Catering',
  'photography': 'Photography',
  'phone_it_repair': 'Phone & IT repair',
  'cleaning': 'Cleaning',
  'other': 'Other',
};

String categoryLabel(String key) {
  final known = serviceCategories[key];
  if (known != null) return known;
  final words = key.replaceAll('_', ' ');
  return words.isEmpty ? key : words[0].toUpperCase() + words.substring(1);
}

const jobTypes = <String, String>{
  'full_time': 'Full time',
  'part_time': 'Part time',
  'casual': 'Casual',
  'contract': 'Contract',
};

const payPeriods = <String, String>{
  'hour': 'hour',
  'day': 'day',
  'week': 'week',
  'month': 'month',
  'one_off': 'one-off',
};

final _kes = NumberFormat.decimalPattern('en');

/// "KES 15,000–18,000 / month", "KES 1,000 / day", "From KES 500 / hour".
String? payLabel(int? min, int? max, String? period) {
  if (min == null && max == null) return null;
  final String amount;
  if (min != null && max != null && min != max) {
    amount = 'KES ${_kes.format(min)}–${_kes.format(max)}';
  } else if (min != null && max == null) {
    amount = 'From KES ${_kes.format(min)}';
  } else if (min == null) {
    amount = 'Up to KES ${_kes.format(max)}';
  } else {
    amount = 'KES ${_kes.format(min)}';
  }
  if (period == null) return amount;
  return period == 'one_off' ? '$amount, one-off' : '$amount / ${payPeriods[period] ?? period}';
}

/// "★ 4.8 (12)", or "New" before anyone has reviewed them.
String ratingLabel(double? average, int count) =>
    average == null || count == 0 ? 'New' : '★ ${average.toStringAsFixed(1)} ($count)';

/// The author of a service post: a name, a badge and a rating.
class ServiceAuthor extends Author {
  const ServiceAuthor({
    required super.id,
    required super.displayName,
    required super.isVerifiedNeighbour,
    this.ratingAverage,
    this.ratingCount = 0,
  });

  factory ServiceAuthor.fromJson(Map<String, dynamic> json) => ServiceAuthor(
        id: json['id'] as String,
        displayName: json['display_name'] as String,
        isVerifiedNeighbour: json['is_verified_neighbour'] as bool? ?? false,
        ratingAverage: (json['rating_average'] as num?)?.toDouble(),
        ratingCount: json['rating_count'] as int? ?? 0,
      );

  final double? ratingAverage;
  final int ratingCount;

  String get rating => ratingLabel(ratingAverage, ratingCount);
}

class ServicePost {
  const ServicePost({
    required this.id,
    required this.author,
    required this.kind,
    required this.category,
    required this.title,
    required this.description,
    required this.areaLabel,
    required this.isOpen,
    required this.isMine,
    required this.status,
    required this.createdAt,
    this.rateInfo,
    this.availabilityInfo,
    this.travelKm,
    this.budgetInfo,
    this.jobType,
    this.payMinKes,
    this.payMaxKes,
    this.payPeriod,
    this.distanceBucket,
    this.expiresAt,
    this.myConversationId,
    this.safetyNotice,
    this.heldFor = const [],
  });

  factory ServicePost.fromJson(Map<String, dynamic> json) => ServicePost(
        id: json['id'] as String,
        author: ServiceAuthor.fromJson(Map<String, dynamic>.from(json['author'] as Map)),
        kind: ServiceKind.parse(json['kind'] as String),
        category: json['category'] as String,
        title: json['title'] as String,
        description: json['description'] as String? ?? '',
        rateInfo: json['rate_info'] as String?,
        availabilityInfo: json['availability_info'] as String?,
        travelKm: json['travel_km'] as int?,
        budgetInfo: json['budget_info'] as String?,
        jobType: json['job_type'] as String?,
        payMinKes: json['pay_min_kes'] as int?,
        payMaxKes: json['pay_max_kes'] as int?,
        payPeriod: json['pay_period'] as String?,
        areaLabel: AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
        distanceBucket: json['distance_bucket'] as String?,
        isOpen: json['is_open'] as bool,
        expiresAt: json['expires_at'] == null ? null : DateTime.parse(json['expires_at'] as String),
        isMine: json['is_mine'] as bool? ?? false,
        myConversationId: json['my_conversation_id'] as String?,
        safetyNotice: json['safety_notice'] as String?,
        status: json['status'] as String,
        heldFor: [for (final phrase in (json['held_for'] as List?) ?? const []) '$phrase'],
        createdAt: DateTime.parse(json['created_at'] as String),
      );

  final String id;
  final ServiceAuthor author;
  final ServiceKind kind;
  final String category;
  final String title;
  final String description;
  final String? rateInfo;
  final String? availabilityInfo;
  final int? travelKm;
  final String? budgetInfo;
  final String? jobType;
  final int? payMinKes;
  final int? payMaxKes;
  final String? payPeriod;
  final AreaLabel areaLabel;
  final String? distanceBucket;
  final bool isOpen;
  final DateTime? expiresAt;
  final bool isMine;
  final String? myConversationId;
  final String? safetyNotice;
  final String status;
  final List<String> heldFor;
  final DateTime createdAt;

  bool get isHeld => status == 'hidden_pending_review';
  String get categoryName => categoryLabel(category);
  String? get pay => payLabel(payMinKes, payMaxKes, payPeriod);
  String? get jobTypeName => jobType == null ? null : (jobTypes[jobType] ?? jobType);

  /// Area · distance band, as every place in the app shows where someone is.
  String get where => [areaLabel.label, ?distanceLabel(distanceBucket)].join(' · ');
}

class Review {
  const Review({
    required this.id,
    required this.reviewer,
    required this.subjectId,
    required this.contextType,
    required this.contextId,
    required this.rating,
    required this.body,
    required this.createdAt,
    this.replyBody,
  });

  factory Review.fromJson(Map<String, dynamic> json) => Review(
        id: json['id'] as String,
        reviewer: Author.fromJson(Map<String, dynamic>.from(json['reviewer'] as Map)),
        subjectId: json['subject_id'] as String,
        contextType: json['context_type'] as String,
        contextId: json['context_id'] as String,
        rating: json['rating'] as int,
        body: json['body'] as String? ?? '',
        replyBody: json['reply_body'] as String?,
        createdAt: DateTime.parse(json['created_at'] as String),
      );

  final String id;
  final Author reviewer;
  final String subjectId;
  final String contextType;
  final String contextId;
  final int rating;
  final String body;
  final String? replyBody;
  final DateTime createdAt;
}

class ReviewPage extends ResultPage<Review> {
  const ReviewPage(super.items, super.nextCursor, {this.average, this.count = 0});
  final double? average;
  final int count;
}

class ServicePage extends ResultPage<ServicePost> {
  const ServicePage(super.items, super.nextCursor, {required this.radiusKm});
  final double radiusKm;
}
