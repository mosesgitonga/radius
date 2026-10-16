import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../auth_controller.dart';
import '../auth_models.dart';

class CodeScreen extends ConsumerStatefulWidget {
  const CodeScreen({super.key});

  @override
  ConsumerState<CodeScreen> createState() => _CodeScreenState();
}

class _CodeScreenState extends ConsumerState<CodeScreen> {
  final _controller = TextEditingController();
  Timer? _timer;
  int _resendIn = 0;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _startCountdown(ref.read(signInFlowProvider).challenge?.resendAfterSeconds ?? 60);
  }

  @override
  void dispose() {
    _timer?.cancel();
    _controller.dispose();
    super.dispose();
  }

  void _startCountdown(int seconds) {
    _timer?.cancel();
    setState(() => _resendIn = seconds);
    _timer = Timer.periodic(const Duration(seconds: 1), (timer) {
      if (!mounted) return;
      setState(() => _resendIn = (_resendIn - 1).clamp(0, 9999));
      if (_resendIn == 0) timer.cancel();
    });
  }

  Future<void> _verify() async {
    final flow = ref.read(signInFlowProvider);
    final code = _controller.text.trim();
    if (flow.phoneNumber == null || code.length != 6) {
      setState(() => _error = 'Enter the 6-digit code from the SMS.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final outcome = await ref.read(authRepositoryProvider).verifyCode(flow.phoneNumber!, code);
      switch (outcome) {
        case SignedIn():
          ref.read(signInFlowProvider.notifier).reset();
          await ref.read(authControllerProvider.notifier).sessionStarted();
        case SignupRequired(:final signupToken):
          ref.read(signInFlowProvider.notifier).signupRequired(signupToken);
          if (mounted) context.go(Routes.signUp);
      }
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _resend() async {
    final phone = ref.read(signInFlowProvider).phoneNumber;
    if (phone == null) return;
    try {
      final challenge = await ref.read(authRepositoryProvider).requestCode(phone);
      ref.read(signInFlowProvider.notifier).codeSent(phone, challenge);
      _startCountdown(challenge.resendAfterSeconds);
      setState(() => _error = null);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final masked = ref.watch(signInFlowProvider).challenge?.maskedNumber ?? 'your phone';
    return Scaffold(
      appBar: AppBar(),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 8, 24, 24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text('Enter the code', style: text.headlineMedium),
              const SizedBox(height: 12),
              Text('Sent to $masked. It expires in 5 minutes.',
                  style: text.bodyLarge?.copyWith(color: Palette.muted)),
              const SizedBox(height: 32),
              TextField(
                controller: _controller,
                autofocus: true,
                keyboardType: TextInputType.number,
                autofillHints: const [AutofillHints.oneTimeCode],
                inputFormatters: [
                  FilteringTextInputFormatter.digitsOnly,
                  LengthLimitingTextInputFormatter(6),
                ],
                textAlign: TextAlign.center,
                style: text.headlineMedium?.copyWith(letterSpacing: 14),
                decoration: const InputDecoration(
                  hintText: '••••••',
                  semanticCounterText: 'Six-digit code',
                ),
                onChanged: (value) {
                  if (value.length == 6) _verify();
                },
              ),
              FormError(_error),
              const SizedBox(height: 16),
              // Wraps rather than overflows: large system font sizes are common.
              Wrap(
                alignment: WrapAlignment.center,
                crossAxisAlignment: WrapCrossAlignment.center,
                children: [
                  Text("Didn't get it?", style: text.bodyMedium?.copyWith(color: Palette.muted)),
                  TextButton(
                    onPressed: _resendIn == 0 ? _resend : null,
                    child: Text(_resendIn == 0
                        ? 'Send again'
                        : 'Send again in ${_resendIn ~/ 60}:${(_resendIn % 60).toString().padLeft(2, '0')}'),
                  ),
                ],
              ),
              const Spacer(),
              BusyButton(label: 'Continue', busy: _busy, onPressed: _verify),
            ],
          ),
        ),
      ),
    );
  }
}
