import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:myradius/core/theme.dart';

void main() {
  tearDown(() => Palette.use(Brightness.light));

  test('the palette follows the brightness in use', () {
    Palette.use(Brightness.light);
    expect(Palette.cream, PaletteColors.light.cream);
    expect(Palette.isDark, isFalse);

    Palette.use(Brightness.dark);
    expect(Palette.cream, PaletteColors.dark.cream);
    expect(Palette.surface, PaletteColors.dark.surface);
    expect(Palette.isDark, isTrue);
    // Text on the brand colour stays white either way.
    expect(Palette.onPrimary, const Color(0xFFFFFFFF));
  });

  test('a dark theme is dark throughout', () {
    final theme = buildTheme(Brightness.dark);
    expect(theme.brightness, Brightness.dark);
    expect(theme.scaffoldBackgroundColor, PaletteColors.dark.cream);
    expect(theme.textTheme.bodyMedium!.color, PaletteColors.dark.ink);
    expect(buildTheme().scaffoldBackgroundColor, PaletteColors.light.cream);
  });

  test('dark text keeps enough contrast against the dark background', () {
    double luminance(Color c) => c.computeLuminance();
    double contrast(Color a, Color b) {
      final hi = luminance(a) > luminance(b) ? luminance(a) : luminance(b);
      final lo = luminance(a) > luminance(b) ? luminance(b) : luminance(a);
      return (hi + 0.05) / (lo + 0.05);
    }

    const dark = PaletteColors.dark;
    // WCAG AA: 4.5 for body text.
    expect(contrast(dark.ink, dark.cream), greaterThan(4.5));
    expect(contrast(dark.ink, dark.surface), greaterThan(4.5));
    expect(contrast(dark.muted, dark.surface), greaterThan(4.5));
    expect(contrast(PaletteColors.light.muted, PaletteColors.light.surface), greaterThan(4.5));
  });
}
