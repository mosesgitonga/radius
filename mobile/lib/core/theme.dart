import 'package:flutter/material.dart';

/// The "Red soil" palette, agreed in the mockups.
class Palette {
  const Palette._();

  static const primary = Color(0xFFC4502E);
  static const primaryDark = Color(0xFF9E3D20);
  static const glow = Color(0xFFF0805C);
  static const glowTint = Color(0xFFFDE6DA);
  static const ink = Color(0xFF1C2333);
  static const inkSoft = Color(0xFF3A4252);
  static const muted = Color(0xFF565E6E);
  static const cream = Color(0xFFFFF6EE);
  static const sand = Color(0xFFF3E6DA);
  static const line = Color(0xFFEFE3D8);
  static const lineStrong = Color(0xFFDDCBBB);
  static const trust = Color(0xFF0E7C70);
  static const trustInk = Color(0xFF0B6158);
  static const trustTint = Color(0xFFE3F4F1);
  static const danger = Color(0xFFB42318);
  static const white = Color(0xFFFFFFFF);
}

/// Both typefaces are variable fonts. Flutter does not map `fontWeight` onto a
/// variable font's weight axis by itself, so every style sets the axis too.
TextStyle _figtree(double size, FontWeight weight, {double? height, Color? color}) {
  return TextStyle(
    fontFamily: 'Figtree',
    fontSize: size,
    fontWeight: weight,
    fontVariations: [FontVariation.weight(weight.value.toDouble())],
    height: height,
    color: color,
  );
}

TextStyle _display(double size, {double letterSpacing = -0.4}) {
  return TextStyle(
    fontFamily: 'Bricolage Grotesque',
    fontSize: size,
    fontWeight: FontWeight.w700,
    fontVariations: const [FontVariation.weight(700)],
    letterSpacing: letterSpacing,
    height: 1.12,
    color: Palette.ink,
  );
}

ThemeData buildTheme() {
  const scheme = ColorScheme(
    brightness: Brightness.light,
    primary: Palette.primary,
    onPrimary: Palette.white,
    primaryContainer: Palette.glowTint,
    onPrimaryContainer: Palette.primaryDark,
    secondary: Palette.trust,
    onSecondary: Palette.white,
    secondaryContainer: Palette.trustTint,
    onSecondaryContainer: Palette.trustInk,
    error: Palette.danger,
    onError: Palette.white,
    surface: Palette.cream,
    onSurface: Palette.ink,
    onSurfaceVariant: Palette.muted,
    surfaceContainerLowest: Palette.white,
    surfaceContainerHighest: Palette.sand,
    outline: Palette.lineStrong,
    outlineVariant: Palette.line,
  );

  final textTheme = TextTheme(
    displaySmall: _display(34),
    headlineMedium: _display(30),
    headlineSmall: _display(26),
    titleLarge: _display(22, letterSpacing: -0.2),
    titleMedium: _figtree(17, FontWeight.w600, color: Palette.ink),
    titleSmall: _figtree(15, FontWeight.w600, color: Palette.ink),
    bodyLarge: _figtree(16, FontWeight.w400, height: 1.5, color: Palette.ink),
    bodyMedium: _figtree(15, FontWeight.w400, height: 1.45, color: Palette.ink),
    bodySmall: _figtree(13, FontWeight.w400, height: 1.45, color: Palette.muted),
    labelLarge: _figtree(16, FontWeight.w600),
    labelMedium: _figtree(14, FontWeight.w500),
    labelSmall: _figtree(12, FontWeight.w500, color: Palette.muted),
  );

  final rounded = RoundedRectangleBorder(borderRadius: BorderRadius.circular(14));

  return ThemeData(
    useMaterial3: true,
    colorScheme: scheme,
    scaffoldBackgroundColor: Palette.cream,
    fontFamily: 'Figtree',
    textTheme: textTheme,
    appBarTheme: AppBarTheme(
      backgroundColor: Palette.cream,
      foregroundColor: Palette.ink,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: false,
      titleTextStyle: _figtree(17, FontWeight.w600, color: Palette.ink),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(56),
        backgroundColor: Palette.primary,
        foregroundColor: Palette.white,
        disabledBackgroundColor: Palette.lineStrong,
        disabledForegroundColor: Palette.white,
        textStyle: _figtree(17, FontWeight.w600),
        shape: rounded,
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(52),
        foregroundColor: Palette.ink,
        side: const BorderSide(color: Palette.lineStrong, width: 1.5),
        textStyle: _figtree(16, FontWeight.w600),
        shape: rounded,
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: Palette.primary,
        textStyle: _figtree(15, FontWeight.w600),
        minimumSize: const Size(44, 44),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: Palette.white,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      hintStyle: _figtree(16, FontWeight.w400, color: Palette.muted),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Palette.lineStrong, width: 1.5),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Palette.lineStrong, width: 1.5),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Palette.primary, width: 2),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: const BorderSide(color: Palette.danger, width: 1.5),
      ),
    ),
    checkboxTheme: CheckboxThemeData(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(5)),
      side: const BorderSide(color: Palette.lineStrong, width: 1.5),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: Palette.white,
      indicatorColor: Palette.glowTint,
      height: 72,
      labelTextStyle: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return _figtree(12, selected ? FontWeight.w600 : FontWeight.w500,
            color: selected ? Palette.primary : Palette.muted);
      }),
      iconTheme: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return IconThemeData(color: selected ? Palette.primary : Palette.muted);
      }),
    ),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: Palette.ink,
      contentTextStyle: _figtree(14, FontWeight.w500, color: Palette.white),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
