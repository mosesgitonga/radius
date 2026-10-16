import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/router.dart';
import 'package:myradius/features/auth/auth_controller.dart';
import 'package:myradius/features/auth/auth_models.dart';
import 'package:myradius/features/auth/phone.dart';

void main() {
  group('normaliseKenyanMobile', () {
    for (final typed in [
      '0712 345 678',
      '712345678',
      '254712345678',
      '+254 712 345 678',
      '0712-345-678',
    ]) {
      test('accepts "$typed"', () {
        expect(normaliseKenyanMobile(typed), '+254712345678');
      });
    }

    test('accepts the 01 range', () {
      expect(normaliseKenyanMobile('0110 123 456'), '+254110123456');
    });

    for (final typed in ['', '12345', '0201234567', '+255712345678', 'abc', '07123456789']) {
      test('rejects "$typed"', () => expect(normaliseKenyanMobile(typed), isNull));
    }
  });

  group('redirectFor', () {
    const me = Me(
      id: 'u1',
      displayName: 'Amina K.',
      status: 'active',
      hasHomeLocation: true,
      isVerifiedNeighbour: false,
    );

    test('restoring waits on the splash', () {
      expect(redirectFor(const AuthRestoring(), Routes.feed), Routes.restoring);
      expect(redirectFor(const AuthRestoring(), Routes.restoring), isNull);
    });

    test('signed out may only be in the sign-in flow', () {
      expect(redirectFor(const AuthSignedOut(), Routes.feed), Routes.signIn);
      expect(redirectFor(const AuthSignedOut(), Routes.me), Routes.signIn);
      expect(redirectFor(const AuthSignedOut(), Routes.code), isNull);
      expect(redirectFor(const AuthSignedOut(), Routes.signUp), isNull);
    });

    test('no home yet means the home-area screen, whatever was asked', () {
      expect(redirectFor(const AuthNeedsHome(me), Routes.feed), Routes.homeArea);
      expect(redirectFor(const AuthNeedsHome(me), Routes.homeArea), isNull);
    });

    test('signed in skips the gates and keeps the tabs', () {
      expect(redirectFor(const AuthSignedIn(me), Routes.signIn), Routes.feed);
      expect(redirectFor(const AuthSignedIn(me), Routes.homeArea), Routes.feed);
      expect(redirectFor(const AuthSignedIn(me), Routes.restoring), Routes.feed);
      expect(redirectFor(const AuthSignedIn(me), Routes.me), isNull);
    });

    test('offline shows the offline screen', () {
      expect(redirectFor(const AuthOffline(), Routes.feed), Routes.offline);
    });
  });
}
