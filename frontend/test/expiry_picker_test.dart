import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/presentation/agent_work_surface.dart';

void main() {
  testWidgets('optional expiry can be selected locally', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        home: Scaffold(
          body: Center(
            child: ConstrainedBox(
              constraints: const BoxConstraints(maxWidth: 390, maxHeight: 600),
              child: AgentWorkSurface(
                items: const [(name: '衛生紙', quantity: '3 包', location: '儲藏櫃')],
                onClose: () {},
              ),
            ),
          ),
        ),
      ),
    );
    expect(find.text('選擇日期（選填）'), findsOneWidget);
    await tester.tap(find.text('選擇日期（選填）'));
    await tester.pumpAndSettle();
    await tester.tap(find.text('OK'));
    await tester.pumpAndSettle();
    expect(find.text('選擇日期（選填）'), findsNothing);
    final today = DateTime.now();
    final formatted =
        '${today.year.toString().padLeft(4, '0')}-${today.month.toString().padLeft(2, '0')}-${today.day.toString().padLeft(2, '0')}';
    expect(find.text(formatted), findsOneWidget);
    expect(find.text('衛生紙'), findsOneWidget);
  });
}
