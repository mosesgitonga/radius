import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';

/// Completing sign-up for a verified new number: a display name, and each
/// consent asked for separately, as the Data Protection Act requires. Marketing
/// is optional and starts unticked.
class SignupScreen extends ConsumerStatefulWidget {
  const SignupScreen({super.key});

  @override
  ConsumerState<SignupScreen> createState() => _SignupScreenState();
}

class _SignupScreenState extends ConsumerState<SignupScreen> {
  final _name = TextEditingController();
  bool _adult = false;
  bool _terms = false;
  bool _privacy = false;
  bool _location = false;
  bool _marketing = false;
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _name.dispose();
    super.dispose();
  }

  bool get _ready =>
      _name.text.trim().length >= 2 && _adult && _terms && _privacy && _location;

  Future<void> _create() async {
    final token = ref.read(signInFlowProvider).signupToken;
    if (token == null) {
      context.go(Routes.signIn);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await ref.read(authRepositoryProvider).signUp(
            signupToken: token,
            displayName: _name.text.trim(),
            acceptMarketing: _marketing,
          );
      ref.read(signInFlowProvider.notifier).reset();
      await ref.read(authControllerProvider.notifier).sessionStarted();
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: const Text('Create your account')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
          children: [
            Text('What should neighbours call you?', style: text.titleMedium),
            const SizedBox(height: 10),
            TextField(
              controller: _name,
              textCapitalization: TextCapitalization.words,
              maxLength: 50,
              autofillHints: const [AutofillHints.name],
              onChanged: (_) => setState(() {}),
              decoration: const InputDecoration(hintText: 'e.g. Wanjiku M.'),
            ),
            Text(
              'Shown on your posts. Use as much or as little of your name as you like.',
              style: text.bodySmall,
            ),
            const SizedBox(height: 24),
            _Consent(
              value: _adult,
              onChanged: (v) => setState(() => _adult = v),
              label: 'I am 18 or older',
            ),
            _Consent(
              value: _terms,
              onChanged: (v) => setState(() => _terms = v),
              label: 'I agree to the terms of use',
            ),
            _Consent(
              value: _privacy,
              onChanged: (v) => setState(() => _privacy = v),
              label: 'I agree to the privacy policy',
            ),
            _Consent(
              value: _location,
              onChanged: (v) => setState(() => _location = v),
              label: 'Use my home area to show what is nearby',
              detail: 'Only the area is kept, never your exact location.',
            ),
            _Consent(
              value: _marketing,
              onChanged: (v) => setState(() => _marketing = v),
              label: 'Send me occasional news about MyRadius',
              detail: 'Optional. You can change this any time.',
            ),
            FormError(_error),
            const SizedBox(height: 24),
            BusyButton(label: 'Create account', busy: _busy, onPressed: _ready ? _create : null),
          ],
        ),
      ),
    );
  }
}

class _Consent extends StatelessWidget {
  const _Consent({required this.value, required this.onChanged, required this.label, this.detail});

  final bool value;
  final ValueChanged<bool> onChanged;
  final String label;
  final String? detail;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return CheckboxListTile(
      value: value,
      onChanged: (v) => onChanged(v ?? false),
      controlAffinity: ListTileControlAffinity.leading,
      contentPadding: EdgeInsets.zero,
      activeColor: Palette.primary,
      title: Text(label, style: text.bodyMedium),
      subtitle: detail == null ? null : Text(detail!, style: text.bodySmall),
    );
  }
}
