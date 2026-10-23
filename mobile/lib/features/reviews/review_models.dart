import '../feed/feed_models.dart';

/// A review of someone, about one listing or service post.
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
