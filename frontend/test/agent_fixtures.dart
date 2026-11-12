import 'package:nestock/features/agent/domain/agent_client.dart';
import 'package:nestock/features/agent/domain/agent_view_data.dart';

class FixtureAgentClient implements AgentClient {
  FixtureAgentClient(this.reply);

  final AgentReply reply;

  @override
  Future<AgentReply> send(String message, {String? replacesApprovalId}) async =>
      reply;

  @override
  Future<AgentReply> resume({
    required String approvalId,
    required bool approved,
    String? selectedOptionId,
    List<String>? approvedOperationIds,
  }) async => reply;
}

AgentReply visionFixtureReply({bool many = false}) => AgentReply(
  message: '照片辨識示範',
  mockVisionItems: [
    (name: '衛生紙', quantity: '3 包', location: '儲藏櫃'),
    (name: '洗衣精', quantity: '1 瓶', location: '洗衣間'),
    (name: '咖啡', quantity: '2 包', location: '廚房'),
    if (many) ...[
      (name: '茶', quantity: '1 盒', location: '廚房'),
      (name: '肥皂', quantity: '2 個', location: '浴室'),
      (name: '米', quantity: '1 包', location: '廚房'),
      (name: '麵', quantity: '3 包', location: '櫃子'),
    ],
  ],
);

const confirmationFixtureReply = AgentReply(
  message: '確認卡片示範',
  mockConfirmationItems: [
    ConfirmationItemView(
      name: '高纖無糖豆漿',
      quantity: 1,
      unit: '瓶',
      location: '冰箱',
    ),
    ConfirmationItemView(name: '衛生紙', quantity: 2, unit: '包', location: '儲藏櫃'),
  ],
);
