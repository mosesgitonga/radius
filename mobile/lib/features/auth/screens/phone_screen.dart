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
import '../phone.dart';

class PhoneScreen extends ConsumerStatefulWidget {
  const PhoneScreen({super.key});

  @override
  ConsumerState<PhoneScreen> createState() => _PhoneScreenState();
}

class _PhoneScreenState extends ConsumerState<PhoneScreen> {
  final _controller = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _controller.dispose();
    super.dispose();
  }

  Future<void> _sendCode() async {
    final phone = normaliseKenyanMobile(_controller.text);
    if (phone == null) {
      setState(() => _error = 'Enter a Kenyan mobile number, like 0712 345 678.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final challenge = await ref.read(authRepositoryProvider).requestCode(phone);
      ref.read(signInFlowProvider.notifier).codeSent(phone, challenge);
      if (mounted) context.push(Routes.code);
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
      body: SafeArea(
        child: LayoutBuilder(
          builder: (context, constraints) => SingleChildScrollView(
            padding: const EdgeInsets.fromLTRB(24, 32, 24, 24),
            child: ConstrainedBox(
              constraints: BoxConstraints(minHeight: constraints.maxHeight - 56),
              child: IntrinsicHeight(
                child: Column(
                  crossAxisAlignment: CrossAxisAlignment.stretch,
                  children: [
                    const Align(alignment: Alignment.centerLeft, child: Wordmark()),
                    const SizedBox(height: 56),
                    Text("What's happening around you", style: text.displaySmall),
                    const SizedBox(height: 14),
                    Text(
                      'News, help, things for sale and events from people who live near you.',
                      style: text.bodyLarge?.copyWith(color: Palette.muted),
                    ),
                    const SizedBox(height: 40),
                    Text('Your phone number', style: text.titleSmall),
                    const SizedBox(height: 10),
                    TextField(
                      controller: _controller,
                      keyboardType: TextInputType.phone,
                      autofillHints: const [AutofillHints.telephoneNumberNational],
                      inputFormatters: [FilteringTextInputFormatter.allow(RegExp(r'[0-9+ ]'))],
                      style: text.bodyLarge?.copyWith(fontSize: 18, letterSpacing: 0.4),
                      textInputAction: TextInputAction.done,
                      onSubmitted: (_) => _sendCode(),
                      decoration: InputDecoration(
                        hintText: '712 345 678',
                        prefixIcon: Padding(
                          padding: const EdgeInsets.only(left: 16, right: 10),
                          child: Center(
                            widthFactor: 1,
                            child: Text('+254', style: text.titleSmall?.copyWith(fontSize: 16)),
                          ),
                        ),
                      ),
                    ),
                    const SizedBox(height: 10),
                    Text(
                      "We'll send a 6-digit code by SMS. Your number is never shown to neighbours.",
                      style: text.bodySmall,
                    ),
                    FormError(_error),
                    const Spacer(),
                    const SizedBox(height: 24),
                    BusyButton(label: 'Send code', busy: _busy, onPressed: _sendCode),
                    const SizedBox(height: 14),
                    Text(
                      'For adults 18 and over. You will be asked to agree to the terms '
                      'and privacy policy before your account is created.',
                      textAlign: TextAlign.center,
                      style: text.bodySmall?.copyWith(fontSize: 12),
                    ),
                  ],
                ),
              ),
            ),
          ),
        ),
      ),
    );
  }
}
