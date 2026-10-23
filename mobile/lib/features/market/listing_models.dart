import 'package:intl/intl.dart';

import '../auth/auth_models.dart';
import '../feed/feed_models.dart';

/// Labels for the server's seeded categories. The list is configurable on the
/// server; a key the app does not know is shown as plain words.
const listingCategories = <String, String>{
  'electronics': 'Electronics',
  'phones': 'Phones',
  'furniture': 'Furniture',
  'household': 'Household',
  'clothing_and_shoes': 'Clothes & shoes',
  'vehicles': 'Vehicles',
  'motorbikes': 'Motorbikes',
  'farm_produce': 'Farm produce',
  'livestock': 'Livestock',
  'books': 'Books',
  'baby_and_kids': 'Baby & kids',
  'beauty': 'Beauty',
  'sports': 'Sports',
  'other': 'Other',
};

String listingCategoryLabel(String key) {
  final known = listingCategories[key];
  if (known != null) return known;
  final words = key.replaceAll('_', ' ');
  return words.isEmpty ? key : words[0].toUpperCase() + words.substring(1);
}

const listingConditions = <String, String>{
  'new': 'New',
  'like_new': 'Like new',
  'good': 'Good',
  'fair': 'Fair',
  'for_parts': 'For parts',
};

/// The ways a listing is priced.
const listingPricings = <String, String>{
  'fixed': 'Fixed price',
  'negotiable': 'Negotiable',
  'free': 'Free',
};

/// The sorts the server offers, in the order the app lists them.
const listingSorts = <String, String>{
  'newest': 'Newest',
  'nearest': 'Nearest',
  'price_asc': 'Price: low to high',
  'price_desc': 'Price: high to low',
};

final _kes = NumberFormat.decimalPattern('en');

/// "KES 18,000", or "Free".
String priceLabel(String pricing, int priceKes) =>
    pricing == 'free' || priceKes == 0 ? 'Free' : 'KES ${_kes.format(priceKes)}';

class Listing {
  const Listing({
    required this.id,
    required this.seller,
    required this.title,
    required this.description,
    required this.category,
    required this.condition,
    required this.pricing,
    required this.priceKes,
    required this.images,
    required this.areaLabel,
    required this.availability,
    required this.isExpired,
    required this.expiresAt,
    required this.isMine,
    required this.saved,
    required this.status,
    required this.createdAt,
    this.distanceBucket,
    this.soldAt,
    this.myConversationId,
    this.reachEstimate,
  });

  factory Listing.fromJson(Map<String, dynamic> json) => Listing(
        id: json['id'] as String,
        seller: Author.fromJson(Map<String, dynamic>.from(json['seller'] as Map)),
        title: json['title'] as String,
        description: json['description'] as String? ?? '',
        category: json['category'] as String,
        condition: json['condition'] as String,
        pricing: json['pricing'] as String,
        priceKes: json['price_kes'] as int? ?? 0,
        images: [
          for (final image in (json['images'] as List?) ?? const [])
            PostImage.fromJson(Map<String, dynamic>.from(image as Map)),
        ],
        areaLabel: AreaLabel.fromJson(Map<String, dynamic>.from(json['area_label'] as Map)),
        distanceBucket: json['distance_bucket'] as String?,
        availability: json['availability'] as String,
        isExpired: json['is_expired'] as bool? ?? false,
        expiresAt: DateTime.parse(json['expires_at'] as String),
        soldAt: json['sold_at'] == null ? null : DateTime.parse(json['sold_at'] as String),
        isMine: json['is_mine'] as bool? ?? false,
        saved: json['saved'] as bool? ?? false,
        myConversationId: json['my_conversation_id'] as String?,
        status: json['status'] as String,
        reachEstimate: json['reach_estimate'] as int?,
        createdAt: DateTime.parse(json['created_at'] as String),
      );

  final String id;
  final Author seller;
  final String title;
  final String description;
  final String category;

  /// `new`, `like_new`, `good`, `fair` or `for_parts`.
  final String condition;

  /// `fixed`, `negotiable` or `free`.
  final String pricing;
  final int priceKes;
  final List<PostImage> images;
  final AreaLabel areaLabel;
  final String? distanceBucket;

  /// `available`, `reserved` or `sold`.
  final String availability;
  final bool isExpired;
  final DateTime expiresAt;
  final DateTime? soldAt;
  final bool isMine;
  final bool saved;
  final String? myConversationId;
  final String status;
  final int? reachEstimate;
  final DateTime createdAt;

  bool get isHeld => status == 'hidden_pending_review';
  bool get isSold => availability == 'sold';
  bool get isReserved => availability == 'reserved';

  /// Someone may still ask about it.
  bool get isOpen => !isSold && !isExpired && !isHeld;

  String get price => priceLabel(pricing, priceKes);
  String get categoryName => listingCategoryLabel(category);
  String get conditionName => listingConditions[condition] ?? condition;

  /// Area · distance band, as everywhere else. Never a number.
  String get where => [areaLabel.label, ?distanceLabel(distanceBucket)].join(' · ');

  Listing copyWith({bool? saved}) => Listing(
        id: id,
        seller: seller,
        title: title,
        description: description,
        category: category,
        condition: condition,
        pricing: pricing,
        priceKes: priceKes,
        images: images,
        areaLabel: areaLabel,
        distanceBucket: distanceBucket,
        availability: availability,
        isExpired: isExpired,
        expiresAt: expiresAt,
        soldAt: soldAt,
        isMine: isMine,
        saved: saved ?? this.saved,
        myConversationId: myConversationId,
        status: status,
        reachEstimate: reachEstimate,
        createdAt: createdAt,
      );
}

class ListingPage extends ResultPage<Listing> {
  const ListingPage(super.items, super.nextCursor, {this.radiusKm});
  final double? radiusKm;
}

/// Someone who asked about a listing: who it might be marked sold to.
class Buyer {
  const Buyer({required this.user, required this.conversationId, required this.accepted});

  factory Buyer.fromJson(Map<String, dynamic> json) => Buyer(
        user: Author.fromJson(Map<String, dynamic>.from(json['user'] as Map)),
        conversationId: json['conversation_id'] as String,
        accepted: json['accepted'] as bool? ?? false,
      );

  final Author user;
  final String conversationId;
  final bool accepted;
}
