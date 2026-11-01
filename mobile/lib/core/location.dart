import 'package:geolocator/geolocator.dart';

/// Where the phone is now, as the server is told it: once, then forgotten.
class Reading {
  const Reading({required this.latitude, required this.longitude, required this.accuracyMetres, this.isMocked = false});

  final double latitude;
  final double longitude;
  final double accuracyMetres;
  final bool isMocked;

  Map<String, dynamic> toJson() => {
    'latitude': latitude,
    'longitude': longitude,
    'accuracy_metres': accuracyMetres,
    'is_mocked': isMocked,
  };
}

/// Why there is no reading, in words for the person holding the phone.
class LocationProblem implements Exception {
  const LocationProblem(this.message);
  final String message;

  @override
  String toString() => message;
}

/// Home is found only by GPS, never chosen (decision M.7). So a reading must be
/// fresh, accurate to [maxAccuracyMetres], and not from a fake-location app. The
/// server checks the same; this says so before anything is sent.
class LocationReader {
  const LocationReader();

  static const maxAccuracyMetres = 150.0;

  /// A reading of where the phone is now. With [mayAsk] false, nothing is asked:
  /// no permission prompt, no reading — used by the daily check, which must never
  /// nag.
  Future<Reading> readHere({bool mayAsk = true}) async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      throw const LocationProblem('Location is turned off. Turn it on in your phone settings and try again.');
    }
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied && mayAsk) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.denied || permission == LocationPermission.unableToDetermine) {
      throw const LocationProblem('MyRadius needs your location to find your area.');
    }
    if (permission == LocationPermission.deniedForever) {
      throw const LocationProblem('Location permission is off for MyRadius. Allow it in Settings › Apps › MyRadius.');
    }
    final Position position;
    try {
      position = await Geolocator.getCurrentPosition(
        locationSettings: const LocationSettings(accuracy: LocationAccuracy.best, timeLimit: Duration(seconds: 25)),
      );
    } catch (_) {
      throw const LocationProblem("Couldn't read your location. Step outside or near a window and try again.");
    }
    if (position.isMocked) {
      throw const LocationProblem(
        'Your phone is using a fake location. Turn off any location-changing app and try again.',
      );
    }
    if (position.accuracy > maxAccuracyMetres) {
      throw const LocationProblem(
        "We couldn't get an accurate enough fix. Turn on precise location, step outside or near a window, "
        'and try again.',
      );
    }
    return Reading(latitude: position.latitude, longitude: position.longitude, accuracyMetres: position.accuracy);
  }
}
