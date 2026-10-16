/// An error from the API, unwrapped from its envelope:
/// `{"error": {"code": ..., "message": ..., "details": {...}}}`.
///
/// The server's messages are written for people, so screens show [message]
/// directly. [code] is for the cases a screen handles specially.
class ApiException implements Exception {
  const ApiException({
    required this.code,
    required this.message,
    this.statusCode,
    this.details = const {},
    this.retryAfterSeconds,
  });

  /// No response at all: offline, a timeout, the server unreachable.
  const ApiException.network()
      : code = 'network',
        message = "Can't reach MyRadius. Check your connection and try again.",
        statusCode = null,
        details = const {},
        retryAfterSeconds = null;

  final String code;
  final String message;
  final int? statusCode;
  final Map<String, dynamic> details;
  final int? retryAfterSeconds;

  bool get isNetwork => code == 'network';
  bool get isUnauthenticated => statusCode == 401;
  bool get isRateLimited => statusCode == 429;

  /// Builds one from a response body, falling back to a plain message for
  /// anything that is not the envelope (a proxy's error page, say).
  factory ApiException.fromResponse(int? statusCode, Object? body, {int? retryAfter}) {
    if (body is Map && body['error'] is Map) {
      final error = body['error'] as Map;
      final details = error['details'];
      return ApiException(
        code: '${error['code'] ?? 'error'}',
        message: '${error['message'] ?? 'Something went wrong.'}',
        statusCode: statusCode,
        details: details is Map ? Map<String, dynamic>.from(details) : const {},
        retryAfterSeconds: retryAfter,
      );
    }
    return ApiException(
      code: 'error',
      message: 'Something went wrong. Please try again.',
      statusCode: statusCode,
      retryAfterSeconds: retryAfter,
    );
  }

  @override
  String toString() => 'ApiException($code, $statusCode): $message';
}
