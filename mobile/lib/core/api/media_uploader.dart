import 'dart:async';
import 'dart:typed_data';

import 'package:dio/dio.dart';

import 'api_client.dart';
import 'api_exception.dart';

/// Uploads photos, for posts and messages alike.
class MediaUploader {
  MediaUploader(this._api, {Dio? storage}) : _storage = storage ?? Dio();

  final ApiClient _api;

  /// Talks to object storage directly, by the signed link the API issues:
  /// never sends the API's token there.
  final Dio _storage;

  /// Uploads one photo and waits until the server has processed it (resized,
  /// location data stripped). Returns its media ID. [purpose] is `post` or
  /// `message`.
  Future<String> uploadPhoto(
    Uint8List bytes, {
    required String purpose,
    String contentType = 'image/jpeg',
    Duration timeout = const Duration(seconds: 45),
  }) async {
    final ticket = Map<String, dynamic>.from(await _api.post('/media/uploads',
        body: {'purpose': purpose, 'content_type': contentType}) as Map);
    final mediaId = ticket['media_id'] as String;
    final maxBytes = ticket['max_bytes'] as int;
    if (bytes.length > maxBytes) {
      throw ApiException(
        code: 'too_large',
        message: 'That photo is too big. Choose one under ${maxBytes ~/ (1024 * 1024)} MB.',
      );
    }
    try {
      final response = await _storage.request<dynamic>(
        ticket['upload_url'] as String,
        data: Stream.fromIterable([bytes]),
        options: Options(
          method: (ticket['upload_method'] as String?) ?? 'PUT',
          headers: {
            ...Map<String, dynamic>.from(ticket['upload_headers'] as Map),
            Headers.contentLengthHeader: bytes.length,
          },
          validateStatus: (_) => true,
        ),
      );
      if ((response.statusCode ?? 500) >= 300) {
        throw const ApiException(code: 'upload_failed', message: "The photo didn't upload. Try again.");
      }
    } on DioException {
      throw const ApiException.network();
    }
    await _api.post('/media/$mediaId/complete');

    final deadline = DateTime.now().add(timeout);
    while (DateTime.now().isBefore(deadline)) {
      final media = Map<String, dynamic>.from(await _api.get('/media/$mediaId') as Map);
      switch (media['status']) {
        case 'ready':
          return mediaId;
        case 'rejected':
          throw ApiException(
            code: 'photo_rejected',
            message: (media['rejection_reason'] as String?) ?? "That photo can't be used.",
          );
      }
      await Future<void>.delayed(const Duration(milliseconds: 800));
    }
    throw const ApiException(code: 'photo_timeout', message: 'The photo is taking too long. Try again.');
  }
}
