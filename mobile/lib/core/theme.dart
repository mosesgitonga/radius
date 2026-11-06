import 'package:flutter/material.dart';

/// One set of the app's colours, for light or dark.
class PaletteColors {
  const PaletteColors({
    required this.primary,
    required this.primaryDark,
    required this.glow,
    required this.glowTint,
    required this.ink,
    required this.inkSoft,
    required this.muted,
    required this.cream,
    required this.sand,
    required this.line,
    required this.lineStrong,
    required this.trust,
    required this.trustInk,
    required this.trustTint,
    required this.danger,
    required this.surface,
  });

  final Color primary;
  final Color primaryDark;
  final Color glow;
  final Color glowTint;
  final Color ink;
  final Color inkSoft;
  final Color muted;
  final Color cream;
  final Color sand;
  final Color line;
  final Color lineStrong;
  final Color trust;
  final Color trustInk;
  final Color trustTint;
  final Color danger;
  final Color surface;

  /// The "Red soil" palette, agreed in the mockups.
  static const light = PaletteColors(
    primary: Color(0xFFC4502E),
    primaryDark: Color(0xFF9E3D20),
    glow: Color(0xFFF0805C),
    glowTint: Color(0xFFFDE6DA),
    ink: Color(0xFF1C2333),
    inkSoft: Color(0xFF3A4252),
    muted: Color(0xFF565E6E),
    cream: Color(0xFFFFF6EE),
    sand: Color(0xFFF3E6DA),
    line: Color(0xFFEFE3D8),
    lineStrong: Color(0xFFDDCBBB),
    trust: Color(0xFF0E7C70),
    trustInk: Color(0xFF0B6158),
    trustTint: Color(0xFFE3F4F1),
    danger: Color(0xFFB42318),
    surface: Color(0xFFFFFFFF),
  );

  /// The same palette at night: warm near-black instead of cream, the red soil a
  /// shade brighter so it still reads, text a warm off-white.
  static const dark = PaletteColors(
    primary: Color(0xFFE0714C),
    primaryDark: Color(0xFFF0A080),
    glow: Color(0xFFF0805C),
    glowTint: Color(0xFF3B2820),
    ink: Color(0xFFF2EAE3),
    inkSoft: Color(0xFFD9CEC5),
    muted: Color(0xFFA9A09A),
    cream: Color(0xFF16120F),
    sand: Color(0xFF2B2420),
    line: Color(0xFF332B26),
    lineStrong: Color(0xFF4B4039),
    trust: Color(0xFF43B9A9),
    trustInk: Color(0xFF8DDCD0),
    trustTint: Color(0xFF16332F),
    danger: Color(0xFFF0705F),
    surface: Color(0xFF211C18),
  );

  static PaletteColors of(Brightness brightness) => brightness == Brightness.dark ? dark : light;
}

/// The colours in use now, light or dark. Set by the app from the theme's
/// brightness before anything below it builds (see `MyRadiusApp`).
class Palette {
  const Palette._();

  static PaletteColors _current = PaletteColors.light;

  static void use(Brightness brightness) => _current = PaletteColors.of(brightness);

  static bool get isDark => identical(_current, PaletteColors.dark);

  static Color get primary => _current.primary;
  static Color get primaryDark => _current.primaryDark;
  static Color get glow => _current.glow;
  static Color get glowTint => _current.glowTint;
  static Color get ink => _current.ink;
  static Color get inkSoft => _current.inkSoft;
  static Color get muted => _current.muted;
  static Color get cream => _current.cream;
  static Color get sand => _current.sand;
  static Color get line => _current.line;
  static Color get lineStrong => _current.lineStrong;
  static Color get trust => _current.trust;
  static Color get trustInk => _current.trustInk;
  static Color get trustTint => _current.trustTint;
  static Color get danger => _current.danger;

  /// Cards, sheets and fields: white by day, a raised dark brown at night.
  static Color get surface => _current.surface;

  /// Text and icons on the brand colour or over a photo: white in both modes.
  static const onPrimary = Color(0xFFFFFFFF);
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

TextStyle _display(double size, Color color, {double letterSpacing = -0.4}) {
  return TextStyle(
    fontFamily: 'Bricolage Grotesque',
    fontSize: size,
    fontWeight: FontWeight.w700,
    fontVariations: const [FontVariation.weight(700)],
    letterSpacing: letterSpacing,
    height: 1.12,
    color: color,
  );
}

ThemeData buildTheme([Brightness brightness = Brightness.light]) {
  final p = PaletteColors.of(brightness);
  final scheme = ColorScheme(
    brightness: brightness,
    primary: p.primary,
    onPrimary: Palette.onPrimary,
    primaryContainer: p.glowTint,
    onPrimaryContainer: p.primaryDark,
    secondary: p.trust,
    onSecondary: Palette.onPrimary,
    secondaryContainer: p.trustTint,
    onSecondaryContainer: p.trustInk,
    error: p.danger,
    onError: Palette.onPrimary,
    surface: p.cream,
    onSurface: p.ink,
    onSurfaceVariant: p.muted,
    surfaceContainerLowest: p.surface,
    surfaceContainerHighest: p.sand,
    outline: p.lineStrong,
    outlineVariant: p.line,
  );

  final textTheme = TextTheme(
    displaySmall: _display(34, p.ink),
    headlineMedium: _display(30, p.ink),
    headlineSmall: _display(26, p.ink),
    titleLarge: _display(22, p.ink, letterSpacing: -0.2),
    titleMedium: _figtree(17, FontWeight.w600, color: p.ink),
    titleSmall: _figtree(15, FontWeight.w600, color: p.ink),
    bodyLarge: _figtree(16, FontWeight.w400, height: 1.5, color: p.ink),
    bodyMedium: _figtree(15, FontWeight.w400, height: 1.45, color: p.ink),
    bodySmall: _figtree(13, FontWeight.w400, height: 1.45, color: p.muted),
    labelLarge: _figtree(16, FontWeight.w600),
    labelMedium: _figtree(14, FontWeight.w500),
    labelSmall: _figtree(12, FontWeight.w500, color: p.muted),
  );

  final rounded = RoundedRectangleBorder(borderRadius: BorderRadius.circular(14));

  return ThemeData(
    useMaterial3: true,
    brightness: brightness,
    colorScheme: scheme,
    scaffoldBackgroundColor: p.cream,
    fontFamily: 'Figtree',
    textTheme: textTheme,
    appBarTheme: AppBarTheme(
      backgroundColor: p.cream,
      foregroundColor: p.ink,
      elevation: 0,
      scrolledUnderElevation: 0,
      centerTitle: false,
      titleTextStyle: _figtree(17, FontWeight.w600, color: p.ink),
    ),
    filledButtonTheme: FilledButtonThemeData(
      style: FilledButton.styleFrom(
        minimumSize: const Size.fromHeight(56),
        backgroundColor: p.primary,
        foregroundColor: Palette.onPrimary,
        disabledBackgroundColor: p.lineStrong,
        disabledForegroundColor: Palette.onPrimary,
        textStyle: _figtree(17, FontWeight.w600),
        shape: rounded,
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        minimumSize: const Size.fromHeight(52),
        foregroundColor: p.ink,
        side: BorderSide(color: p.lineStrong, width: 1.5),
        textStyle: _figtree(16, FontWeight.w600),
        shape: rounded,
      ),
    ),
    textButtonTheme: TextButtonThemeData(
      style: TextButton.styleFrom(
        foregroundColor: p.primary,
        textStyle: _figtree(15, FontWeight.w600),
        minimumSize: const Size(44, 44),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: p.surface,
      contentPadding: const EdgeInsets.symmetric(horizontal: 16, vertical: 16),
      hintStyle: _figtree(16, FontWeight.w400, color: p.muted),
      border: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(color: p.lineStrong, width: 1.5),
      ),
      enabledBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(color: p.lineStrong, width: 1.5),
      ),
      focusedBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(color: p.primary, width: 2),
      ),
      errorBorder: OutlineInputBorder(
        borderRadius: BorderRadius.circular(14),
        borderSide: BorderSide(color: p.danger, width: 1.5),
      ),
    ),
    checkboxTheme: CheckboxThemeData(
      shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(5)),
      side: BorderSide(color: p.lineStrong, width: 1.5),
    ),
    navigationBarTheme: NavigationBarThemeData(
      backgroundColor: p.surface,
      indicatorColor: p.glowTint,
      height: 72,
      labelTextStyle: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return _figtree(12, selected ? FontWeight.w600 : FontWeight.w500,
            color: selected ? p.primary : p.muted);
      }),
      iconTheme: WidgetStateProperty.resolveWith((states) {
        final selected = states.contains(WidgetState.selected);
        return IconThemeData(color: selected ? p.primary : p.muted);
      }),
    ),
    snackBarTheme: SnackBarThemeData(
      backgroundColor: p.ink,
      contentTextStyle: _figtree(14, FontWeight.w500, color: p.cream),
      behavior: SnackBarBehavior.floating,
    ),
  );
}
