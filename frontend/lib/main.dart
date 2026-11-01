import 'package:flutter/material.dart';

import 'app.dart';
import 'features/agent/data/mock_agent_client.dart';
import 'shared/data/demo_catalog.dart';

void main() {
  const name = String.fromEnvironment(
    'MOCK_SCENARIO',
    defaultValue: 'singleAction',
  );
  final scenario =
      MockScenario.values.where((value) => value.name == name).firstOrNull ??
      MockScenario.singleAction;
  runApp(
    NestockApp(
      agentClient: MockAgentClient(scenario: scenario),
      catalog: const DemoCatalog(),
    ),
  );
}
