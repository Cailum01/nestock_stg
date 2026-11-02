import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/shared/theme/app_theme.dart';

double contrast(Color a, Color b) {
  final x = a.computeLuminance();
  final y = b.computeLuminance();
  return (x > y ? x + .05 : y + .05) / (x > y ? y + .05 : x + .05);
}

void main() {
  test('brand surfaces and primary text remain readable', () {
    final theme = buildAppTheme();
    final colors = theme.colorScheme;
    expect(theme.scaffoldBackgroundColor, const Color(0xFFF8F6F0));
    expect(colors.primaryContainer, const Color(0xFF566CA5));
    expect(
      contrast(colors.onPrimaryContainer, colors.primaryContainer),
      greaterThanOrEqualTo(4.5),
    );
    expect(
      contrast(colors.onPrimary, colors.primary),
      greaterThanOrEqualTo(4.5),
    );
    expect(
      contrast(colors.onSurface, colors.surface),
      greaterThanOrEqualTo(4.5),
    );
    expect(
      contrast(colors.onSecondaryContainer, colors.secondaryContainer),
      greaterThanOrEqualTo(4.5),
    );
  });
}
