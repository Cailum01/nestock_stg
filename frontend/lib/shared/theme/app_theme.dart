import 'package:flutter/material.dart';

abstract final class AppTokens {
  static const spacing = 16.0;
  static const radius = 12.0;
  static const blue = Color(0xFF566CA5);
  static const cream = Color(0xFFF8F6F0);
  static const darkBlue = Color(0xFF40558D);
  static const ink = Color(0xFF252A38);
  static const muted = Color(0xFF505B70);
  static const paleBlue = Color(0xFFE7EAF2);
  static const card = Color(0xFFFFFDF8);
  static const border = Color(0xFF7A859A);
}

ThemeData buildAppTheme() {
  final scheme = ColorScheme.fromSeed(seedColor: AppTokens.blue).copyWith(
    primary: AppTokens.darkBlue,
    onPrimary: AppTokens.card,
    primaryContainer: AppTokens.blue,
    onPrimaryContainer: AppTokens.card,
    secondary: AppTokens.darkBlue,
    onSecondary: AppTokens.card,
    secondaryContainer: AppTokens.paleBlue,
    onSecondaryContainer: AppTokens.ink,
    surface: AppTokens.cream,
    onSurface: AppTokens.ink,
    onSurfaceVariant: AppTokens.muted,
    surfaceContainerLowest: AppTokens.card,
    surfaceContainerLow: AppTokens.card,
    surfaceContainer: AppTokens.paleBlue,
    surfaceContainerHigh: AppTokens.paleBlue,
    surfaceContainerHighest: AppTokens.paleBlue,
    outline: AppTokens.border,
    outlineVariant: AppTokens.blue,
  );
  final border = OutlineInputBorder(
    borderRadius: BorderRadius.circular(AppTokens.radius),
    borderSide: const BorderSide(color: AppTokens.border),
  );
  return ThemeData(
    colorScheme: scheme,
    scaffoldBackgroundColor: AppTokens.cream,
    useMaterial3: true,
    appBarTheme: const AppBarTheme(
      backgroundColor: AppTokens.cream,
      foregroundColor: AppTokens.ink,
    ),
    cardTheme: CardThemeData(
      color: AppTokens.card,
      elevation: 0,
      shape: RoundedRectangleBorder(
        side: const BorderSide(color: AppTokens.paleBlue),
        borderRadius: BorderRadius.circular(AppTokens.radius),
      ),
    ),
    inputDecorationTheme: InputDecorationTheme(
      filled: true,
      fillColor: AppTokens.card,
      border: border,
      enabledBorder: border,
      focusedBorder: border.copyWith(
        borderSide: const BorderSide(color: AppTokens.darkBlue, width: 2),
      ),
    ),
    outlinedButtonTheme: OutlinedButtonThemeData(
      style: OutlinedButton.styleFrom(
        foregroundColor: AppTokens.darkBlue,
        side: const BorderSide(color: AppTokens.border),
      ),
    ),
  );
}
