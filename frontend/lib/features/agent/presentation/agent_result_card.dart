import 'package:flutter/material.dart';

import '../domain/agent_view_data.dart';

/// Extend with typed renderers after the result payload contract is agreed.
class AgentResultCard extends StatelessWidget {
  const AgentResultCard({super.key, required this.result});
  final AgentResultView result;
  @override
  Widget build(BuildContext context) => Card(
    child: ListTile(
      leading: Icon(switch (result.kind) {
        ResultKind.inventory => Icons.inventory_2_outlined,
        ResultKind.vision => Icons.photo_outlined,
        ResultKind.forecast => Icons.insights_outlined,
        ResultKind.purchaseOptions => Icons.shopping_bag_outlined,
        ResultKind.actionResult => Icons.info_outline,
      }),
      title: Text(result.summary),
    ),
  );
}
