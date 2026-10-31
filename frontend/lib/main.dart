import 'package:flutter/material.dart';

import 'app.dart';
import 'features/agent/data/mock_agent_client.dart';
import 'shared/data/demo_catalog.dart';

void main() {
  runApp(
    NestockApp(agentClient: MockAgentClient(), catalog: const DemoCatalog()),
  );
}
