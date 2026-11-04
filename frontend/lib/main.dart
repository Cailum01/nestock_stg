import 'package:flutter/material.dart';

import 'app.dart';
import 'features/agent/data/mock_agent_client.dart';
import 'features/agent/data/http_agent_client.dart';
import 'shared/data/demo_catalog.dart';

void main() {
  const name = String.fromEnvironment(
    'MOCK_SCENARIO',
    defaultValue: 'conversation',
  );
  final scenario =
      MockScenario.values.where((value) => value.name == name).firstOrNull ??
      MockScenario.conversation;
  runApp(
    NestockApp(
      agentClient:
          const String.fromEnvironment(
                'AGENT_TRANSPORT',
                defaultValue: 'http',
              ) ==
              'mock'
          ? MockAgentClient(scenario: scenario)
          : HttpAgentClient(
              baseUrl: Uri.parse(
                const String.fromEnvironment(
                  'MEDIA_API_BASE',
                  defaultValue: 'http://10.0.2.2:8000',
                ),
              ),
            ),
      catalog: const DemoCatalog(),
    ),
  );
}
