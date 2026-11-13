import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/location.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';

/// Setting the home area. Home is found only by GPS, never chosen (decision M.7),
/// so the app first asks whether the phone is at home. Someone signing up away
/// from home starts from where they are, provisionally, and sets the real home
/// when they get there (decision M.10). The position is read once and sent once;
/// the server keeps only the area's cell, and the app keeps nothing.
class HomeAreaScreen extends ConsumerStatefulWidget {
  const HomeAreaScreen({super.key});

  @override
  ConsumerState<HomeAreaScreen> createState() => _HomeAreaScreenState();
}

class _HomeAreaScreenState extends ConsumerState<HomeAreaScreen> {
  bool _busy = false;
  String? _error;
  String? _areaName;
  bool? _atHome;

  Future<void> _useMyLocation({required bool atHome}) async {
    setState(() {
      _busy = true;
      _error = null;
      _atHome = atHome;
    });
    try {
      final reading = await ref.read(locationReaderProvider).readHere();
      final area = await ref.read(authRepositoryProvider).setHome(reading, atHome: atHome);
      // Too few neighbours yet to name the place safely: say "your area" instead.
      setState(() => _areaName = area.precision == 'generic' ? 'your area' : area.label);
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
    final away = _atHome == false;
    return Scaffold(
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              const Align(alignment: Alignment.centerLeft, child: RadiusMark(size: 56)),
              const SizedBox(height: 28),
              Text(
                !found
                    ? 'Are you at home right now?'
                    : away
                        ? "You're in $_areaName for now"
                        : 'Welcome to $_areaName',
                style: text.headlineMedium,
              ),
              const SizedBox(height: 12),
              Text(
                !found
                    ? "Hoodora finds your area from where your phone is. If you're at home, we'll "
                        "set it now. If you're away, you can start from where you are and set your "
                        'home when you get there.'
                    : away
                        ? 'You can use everything from here for the next week. When you get home, set it '
                            'from Me › Home area. Until then nothing you do here counts '
                            'towards proving where you live.'
                        : 'Your feed, the marketplace and events are built around $_areaName.',
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
                FilledButton(onPressed: _continue, child: Text(away ? 'Continue' : 'This is home'))
              else ...[
                BusyButton(
                  label: "Yes, I'm at home",
                  busy: _busy && _atHome == true,
                  onPressed: _busy ? null : () => _useMyLocation(atHome: true),
                ),
                const SizedBox(height: 10),
                OutlinedButton(
                  onPressed: _busy ? null : () => _useMyLocation(atHome: false),
                  child: const Text("No, I'm away from home"),
                ),
              ],
              if (found) ...[
                const SizedBox(height: 10),
                OutlinedButton(
                  onPressed: _busy
                      ? null
                      : () => setState(() {
                            _areaName = null;
                            _atHome = null;
                          }),
                  child: const Text('Start again'),
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
