import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

void main() {
  test('release metadata has a SemVer version and positive build number', () {
    final pubspec = File('pubspec.yaml').readAsStringSync();
    final match = RegExp(
      r'^version:\s+(\d+\.\d+\.\d+)\+(\d+)\s*$',
      multiLine: true,
    ).firstMatch(pubspec);

    expect(match, isNotNull);
    expect(match!.group(1), '1.0.7');
    expect(match.group(2), '8');
  });
}
