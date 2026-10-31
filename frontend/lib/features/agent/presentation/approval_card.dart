import 'package:flutter/material.dart';

import '../domain/agent_view_data.dart';

/// Mock-only interaction. Real authorization and validation belong in the backend.
class ApprovalCard extends StatelessWidget {
  const ApprovalCard({
    super.key,
    required this.approval,
    required this.busy,
    required this.onDecision,
  });
  final ApprovalView approval;
  final bool busy;
  final ValueChanged<bool> onDecision;
  @override
  Widget build(BuildContext context) => Card(
    child: Padding(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text(switch (approval.presentation) {
            ApprovalPresentation.singleAction => '單一操作確認示範',
            ApprovalPresentation.batchAction => '批次操作確認示範',
            ApprovalPresentation.purchaseOptions => '選購項目確認示範',
          }),
          for (final operation in approval.operations)
            Padding(
              padding: const EdgeInsets.symmetric(vertical: 8),
              child: Text(operation.description),
            ),
          const Text('此示範不會寫入資料，也不會下單或付款。'),
          Wrap(
            spacing: 12,
            children: [
              TextButton(
                onPressed: busy ? null : () => onDecision(false),
                child: const Text('取消'),
              ),
              FilledButton(
                onPressed: busy ? null : () => onDecision(true),
                child: const Text('確認示範'),
              ),
            ],
          ),
        ],
      ),
    ),
  );
}
