import 'package:flutter/material.dart';

abstract final class AppTokens {
  static const spacing = 16.0;
  static const radius = 12.0;
  static const primary = Colors.teal;
}

ThemeData buildAppTheme() => ThemeData(
  colorSchemeSeed: AppTokens.primary,
  useMaterial3: true,
  cardTheme: CardThemeData(
    shape: RoundedRectangleBorder(
      borderRadius: BorderRadius.circular(AppTokens.radius),
    ),
  ),
);
