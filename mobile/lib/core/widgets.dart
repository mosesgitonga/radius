import 'package:flutter/material.dart';

import 'theme.dart';

/// The radius mark: glow rings around a primary centre.
class RadiusMark extends StatelessWidget {
  const RadiusMark({super.key, this.size = 36});

  final double size;

  @override
  Widget build(BuildContext context) {
    return SizedBox.square(
      dimension: size,
      child: CustomPaint(painter: _RadiusMarkPainter()),
    );
  }
}

class _RadiusMarkPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final centre = size.center(Offset.zero);
    final unit = size.shortestSide / 36;
    final ring = Paint()
      ..color = Palette.glow
      ..style = PaintingStyle.stroke;
    canvas.drawCircle(centre, 16 * unit, ring..strokeWidth = 1.5 * unit);
    canvas.drawCircle(centre, 11 * unit, ring..strokeWidth = 2 * unit);
    canvas.drawCircle(centre, 6.5 * unit, Paint()..color = Palette.primary);
    canvas.drawCircle(centre, 2.2 * unit, Paint()..color = Palette.cream);
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}

class Wordmark extends StatelessWidget {
  const Wordmark({super.key});

  @override
  Widget build(BuildContext context) {
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        const RadiusMark(),
        const SizedBox(width: 10),
        Text('MyRadius', style: Theme.of(context).textTheme.titleLarge),
      ],
    );
  }
}

/// A full-width button that shows progress while [busy] and ignores taps then.
class BusyButton extends StatelessWidget {
  const BusyButton({
    super.key,
    required this.label,
    required this.onPressed,
    this.busy = false,
  });

  final String label;
  final VoidCallback? onPressed;
  final bool busy;

  @override
  Widget build(BuildContext context) {
    return FilledButton(
      onPressed: busy ? null : onPressed,
      child: busy
          ? const SizedBox.square(
              dimension: 22,
              child: CircularProgressIndicator(strokeWidth: 2.5, color: Palette.white),
            )
          : Text(label),
    );
  }
}

/// An inline error under a form, in words a person can act on.
class FormError extends StatelessWidget {
  const FormError(this.message, {super.key});

  final String? message;

  @override
  Widget build(BuildContext context) {
    if (message == null) return const SizedBox.shrink();
    return Padding(
      padding: const EdgeInsets.only(top: 12),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.error_outline, size: 18, color: Palette.danger),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              message!,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(color: Palette.danger),
            ),
          ),
        ],
      ),
    );
  }
}

/// The verified-neighbour badge.
class NeighbourBadge extends StatelessWidget {
  const NeighbourBadge({super.key, this.long = false});

  final bool long;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 3),
      decoration: BoxDecoration(
        color: Palette.trustTint,
        borderRadius: BorderRadius.circular(12),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          const Icon(Icons.shield_outlined, size: 13, color: Palette.trustInk),
          const SizedBox(width: 4),
          Text(
            long ? 'Verified neighbour' : 'Neighbour',
            style: Theme.of(context)
                .textTheme
                .labelSmall
                ?.copyWith(color: Palette.trustInk, fontWeight: FontWeight.w600),
          ),
        ],
      ),
    );
  }
}

/// A calm panel with an icon, used for privacy notes and empty states.
class NotePanel extends StatelessWidget {
  const NotePanel({super.key, required this.icon, required this.text, this.tint = Palette.white});

  final IconData icon;
  final String text;
  final Color tint;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(
        color: tint,
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: Palette.line),
      ),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Icon(icon, size: 22, color: Palette.primary),
          const SizedBox(width: 12),
          Expanded(child: Text(text, style: Theme.of(context).textTheme.bodyMedium)),
        ],
      ),
    );
  }
}
