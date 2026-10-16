import 'package:flutter/material.dart';

import '../../core/theme.dart';
import '../../core/widgets.dart';

/// A tab whose feature lands in a later slice. Says so plainly.
class TabPlaceholder extends StatelessWidget {
  const TabPlaceholder({super.key, required this.title, required this.message});

  final String title;
  final String message;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: Text(title, style: text.headlineSmall)),
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(32),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const RadiusMark(size: 56),
              const SizedBox(height: 20),
              Text(
                message,
                textAlign: TextAlign.center,
                style: text.bodyLarge?.copyWith(color: Palette.muted),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
