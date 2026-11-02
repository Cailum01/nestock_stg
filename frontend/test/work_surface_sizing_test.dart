import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:nestock/features/agent/presentation/agent_work_surface.dart';

void main() {
  testWidgets('panel grows with content and caps with internal scrolling', (
    tester,
  ) async {
    const item = (name: '米', quantity: '1 包', location: '廚房');
    Widget panel(int count) => MaterialApp(
      home: Scaffold(
        body: Align(
          alignment: Alignment.bottomCenter,
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxHeight: 500, maxWidth: 390),
            child: AgentWorkSurface(
              items: List.filled(count, item),
              onClose: () {},
            ),
          ),
        ),
      ),
    );
    await tester.pumpWidget(panel(1));
    final shortHeight = tester
        .getSize(find.byKey(const Key('work-surface')))
        .height;
    await tester.pumpWidget(panel(7));
    await tester.pumpAndSettle();
    final tallHeight = tester
        .getSize(find.byKey(const Key('work-surface')))
        .height;
    expect(shortHeight, lessThan(300));
    expect(tallHeight, greaterThan(shortHeight));
    expect(tallHeight, closeTo(500, 1));
    expect(find.text('確認全部入庫').hitTestable(), findsOneWidget);
    final scrollable = tester.state<ScrollableState>(
      find
          .descendant(
            of: find.byKey(const Key('work-items')),
            matching: find.byType(Scrollable),
          )
          .first,
    );
    expect(scrollable.position.maxScrollExtent, greaterThan(0));
  });
}
