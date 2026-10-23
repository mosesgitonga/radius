import '../../core/api/api_client.dart';
import '../chats/chat_models.dart';
import 'listing_models.dart';

/// Listings: browsing, selling, saving, and asking the seller.
class ListingRepository {
  ListingRepository(this._api);

  final ApiClient _api;

  Future<ListingPage> browse({String? query, String? category, String? sort, String? cursor}) async {
    final q = query?.trim();
    final data = Map<String, dynamic>.from(await _api.get('/listings', query: {
      if (q != null && q.isNotEmpty) 'q': q,
      'category': ?category,
      'sort': ?sort,
      'cursor': ?cursor,
    }) as Map);
    return ListingPage(
      [for (final item in data['items'] as List) Listing.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
      radiusKm: (data['radius_km'] as num?)?.toDouble(),
    );
  }

  /// [which] is `mine` or `saved`.
  Future<ListingPage> list(String which, {String? cursor}) async {
    final data = Map<String, dynamic>.from(await _api.get('/listings/$which', query: {'cursor': ?cursor}) as Map);
    return ListingPage(
      [for (final item in data['items'] as List) Listing.fromJson(Map<String, dynamic>.from(item as Map))],
      data['next_cursor'] as String?,
    );
  }

  Future<Listing> listing(String id) async =>
      Listing.fromJson(Map<String, dynamic>.from(await _api.get('/listings/$id') as Map));

  Future<Listing> create(Map<String, dynamic> fields) async =>
      Listing.fromJson(Map<String, dynamic>.from(await _api.post('/listings', body: fields, idempotent: true) as Map));

  Future<Listing> update(String id, Map<String, dynamic> changes) async =>
      Listing.fromJson(Map<String, dynamic>.from(await _api.patch('/listings/$id', body: changes) as Map));

  Future<void> delete(String id) => _api.delete('/listings/$id');

  Future<Listing> renew(String id) async =>
      Listing.fromJson(Map<String, dynamic>.from(await _api.post('/listings/$id/renew') as Map));

  /// [availability] is `available`, `reserved` or `sold`; [buyerId] optionally
  /// names who bought it.
  Future<Listing> setAvailability(String id, String availability, {String? buyerId}) async =>
      Listing.fromJson(Map<String, dynamic>.from(await _api.put('/listings/$id/availability',
          body: {'availability': availability, 'buyer_id': ?buyerId}) as Map));

  Future<List<Buyer>> buyers(String id) async => [
        for (final item in await _api.get('/listings/$id/buyers') as List)
          Buyer.fromJson(Map<String, dynamic>.from(item as Map)),
      ];

  Future<void> save(String id, bool saved) =>
      saved ? _api.put('/listings/$id/save') : _api.delete('/listings/$id/save');

  /// The conversation with the seller about this listing, made if need be.
  Future<Conversation> contact(String id) async =>
      Conversation.fromJson(Map<String, dynamic>.from(await _api.post('/listings/$id/chat') as Map));
}
