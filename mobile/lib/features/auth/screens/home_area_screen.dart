import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:geolocator/geolocator.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';

/// Setting the home area. The phone's position is read once and sent once; the
/// server keeps only the area's cell and answers with its name. The app keeps
/// nothing. (A map to pick the spot by hand comes with the maps slice.)
class HomeAreaScreen extends ConsumerStatefulWidget {
  const HomeAreaScreen({super.key});

  @override
  ConsumerState<HomeAreaScreen> createState() => _HomeAreaScreenState();
}

class _HomeAreaScreenState extends ConsumerState<HomeAreaScreen> {
  bool _busy = false;
  String? _error;
  String? _areaName;

  Future<void> _useMyLocation() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final position = await _currentPosition();
      if (position == null) return;
      final area = await ref.read(authRepositoryProvider).setHome(
            latitude: position.latitude,
            longitude: position.longitude,
            accuracyMetres: position.accuracy,
          );
      setState(() => _areaName = area.label);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  /// The current position, after asking for permission; null, with an
  /// explanation shown, when it can't be had.
  Future<Position?> _currentPosition() async {
    if (!await Geolocator.isLocationServiceEnabled()) {
      setState(() => _error = 'Location is turned off. Turn it on in your phone settings and try again.');
      return null;
    }
    var permission = await Geolocator.checkPermission();
    if (permission == LocationPermission.denied) {
      permission = await Geolocator.requestPermission();
    }
    if (permission == LocationPermission.denied) {
      setState(() => _error = 'MyRadius needs your location once, to find your area.');
      return null;
    }
    if (permission == LocationPermission.deniedForever) {
      setState(() => _error = 'Location permission is off for MyRadius. Allow it in Settings › Apps › MyRadius.');
      return null;
    }
    return Geolocator.getCurrentPosition(
      locationSettings: const LocationSettings(
        accuracy: LocationAccuracy.high,
        timeLimit: Duration(seconds: 20),
      ),
    );
  }

  Future<void> _continue() => ref.read(authControllerProvider.notifier).refreshMe();

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final found = _areaName != null;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Align(alignment: Alignment.centerLeft, child: RadiusMark(size: 56)),
              const SizedBox(height: 28),
              Text(found ? 'Welcome to $_areaName' : "Where's home?", style: text.headlineMedium),
              const SizedBox(height: 12),
              Text(
                found
                    ? 'Your feed, the marketplace and events are built around $_areaName.'
                    : 'Stand at or near home and we\'ll find your area. Your feed, the '
                        'marketplace and events are built around it.',
                style: text.bodyLarge?.copyWith(color: Palette.muted),
              ),
              const SizedBox(height: 20),
              const NotePanel(
                icon: Icons.shield_outlined,
                text: 'We keep only the area, never your exact spot. Neighbours see '
                    'its name and a rough distance like "1–3 km".',
              ),
              FormError(_error),
              const Spacer(),
              if (found)
                FilledButton(onPressed: _continue, child: const Text('This is home'))
              else
                BusyButton(label: 'Use my location', busy: _busy, onPressed: _useMyLocation),
              if (found) ...[
                const SizedBox(height: 10),
                OutlinedButton(
                  onPressed: _busy ? null : _useMyLocation,
                  child: const Text('Try again'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
