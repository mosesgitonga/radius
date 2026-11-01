import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/location.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';

/// Setting the home area. The phone's position is read once and sent once; the
/// server keeps only the area's cell and answers with its name. The app keeps
/// nothing. Home is found only by GPS, never chosen (decision M.7).
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
      final reading = await const LocationReader().readHere();
      final area = await ref.read(authRepositoryProvider).setHome(reading);
      setState(() => _areaName = area.label);
    } on LocationProblem catch (problem) {
      setState(() => _error = problem.message);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
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
