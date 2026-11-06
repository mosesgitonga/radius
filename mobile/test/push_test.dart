import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/push.dart';

void main() {
  test('a tapped notification opens what it is about', () {
    expect(PushService.routeFor({'conversation_id': 'c1', 'type': 'message'}), '/chats/c/c1');
    expect(PushService.routeFor({'type': 'comment', 'target_type': 'post', 'target_id': 'p1'}), '/feed/post/p1');
    expect(PushService.routeFor({'type': 'circle', 'event': 'request', 'user_id': 'u2'}), '/people/u2');
    expect(PushService.routeFor({'type': 'account_notice'}), isNull);
  });

  test('without Firebase set up, push is simply unavailable', () {
    expect(PushService.available, isFalse);
  });
}
