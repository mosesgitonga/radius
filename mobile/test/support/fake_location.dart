import 'package:myradius/core/location.dart';

/// Stands wherever a test says, or fails the way it says. Tests have no GPS.
class FakeReader extends LocationReader {
  const FakeReader({this.problem});

  /// No GPS at all: the guest Home offers sign-up instead.
  const FakeReader.none() : problem = 'MyRadius needs your location to find your area.';

  final String? problem;

  @override
  Future<Reading> readHere({bool mayAsk = true}) async {
    if (problem != null) throw LocationProblem(problem!);
    return const Reading(latitude: -0.4201, longitude: 36.9476, accuracyMetres: 12);
  }
}
