import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../core/router.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';

/// "Join MyRadius to …": what a visitor sees when they try to take part.
Future<void> showJoinSheet(BuildContext context, {required String toDo}) {
  return showModalBottomSheet<void>(
    context: context,
    backgroundColor: Palette.cream,
    builder: (sheet) {
      final text = Theme.of(sheet).textTheme;
      return SafeArea(
        child: Padding(
          padding: const EdgeInsets.fromLTRB(24, 24, 24, 16),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const RadiusMark(size: 44),
              const SizedBox(height: 14),
              Text('Join MyRadius to $toDo', style: text.titleMedium, textAlign: TextAlign.center),
              const SizedBox(height: 6),
              Text(
                'It takes a minute with your phone number. Your neighbours see your name '
                'once you join, never where you live.',
                style: text.bodySmall,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 18),
              SizedBox(
                width: double.infinity,
                child: BusyButton(
                  label: 'Sign up',
                  onPressed: () {
                    Navigator.of(sheet).pop();
                    context.push(Routes.signIn);
                  },
                ),
              ),
              TextButton(onPressed: () => Navigator.of(sheet).pop(), child: const Text('Not now')),
            ],
          ),
        ),
      );
    },
  );
}
