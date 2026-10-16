/// Turns what someone typed into an E.164 Kenyan mobile number, or null.
///
/// Accepts the forms people actually type: `0712 345 678`, `712345678`,
/// `254712345678`, `+254 712 345 678`, and the newer `01…` range. The server
/// normalises and validates again; this is only so the form can say "that
/// doesn't look right" before spending a request.
String? normaliseKenyanMobile(String typed) {
  final digits = typed.replaceAll(RegExp(r'[\s\-().]'), '');
  String? local;
  if (RegExp(r'^\+254\d{9}$').hasMatch(digits)) {
    local = digits.substring(4);
  } else if (RegExp(r'^254\d{9}$').hasMatch(digits)) {
    local = digits.substring(3);
  } else if (RegExp(r'^0\d{9}$').hasMatch(digits)) {
    local = digits.substring(1);
  } else if (RegExp(r'^\d{9}$').hasMatch(digits)) {
    local = digits;
  }
  if (local == null || !(local.startsWith('7') || local.startsWith('1'))) {
    return null;
  }
  return '+254$local';
}
