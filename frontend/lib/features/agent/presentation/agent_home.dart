import 'dart:async';

import 'package:flutter/material.dart';

import '../../../shared/widgets/async_state.dart';
import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import 'agent_result_card.dart';
import 'approval_card.dart';
import 'agent_state.dart';
import '../../photo/photo_client.dart';

class AgentHome extends StatefulWidget {
  const AgentHome({
    super.key,
    required this.client,
    required this.openInventory,
    required this.openShopping,
    this.photoClient = const UnavailablePhotoClient(),
  });
  final AgentClient client;
  final PhotoClient photoClient;
  final void Function(BuildContext) openInventory;
  final void Function(BuildContext) openShopping;
  @override
  State<AgentHome> createState() => _AgentHomeState();
}

class _AgentHomeState extends State<AgentHome> {
  final _input = TextEditingController();
  AgentReply? _reply;
  AgentState _state = AgentState.idle;
  bool get _busy => _state == AgentState.loading;
  Future<AgentReply> Function()? _retry;

  Future<void> _perform(Future<AgentReply> Function() action) async {
    if (_busy) return;
    setState(() {
      _state = AgentState.loading;
      _retry = action;
    });
    try {
      final reply = await action();
      if (!mounted) return;
      setState(() {
        _reply = reply;
        _state = reply.approval == null
            ? AgentState.completed
            : AgentState.waitingForApproval;
        _input.clear();
      });
    } on TimeoutException {
      if (mounted) setState(() => _state = AgentState.timeout);
    } catch (_) {
      if (mounted) setState(() => _state = AgentState.error);
    }
  }

  Future<void> _openPhoto() async {
    try {
      final access = await widget.photoClient.open();
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(
          content: Text(
            access == PhotoAccess.denied ? '相機權限遭拒，請檢查裝置設定。' : '拍照與選取照片功能尚未開放。',
          ),
        ),
      );
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('無法開啟相機，請稍後再試。')));
      }
    }
  }

  void _send() {
    final message = _input.text.trim();
    if (message.isEmpty || _reply?.approval != null) return;
    _perform(() => widget.client.send(message));
  }

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final approval = _reply?.approval;
    return Scaffold(
      appBar: AppBar(title: const Text('Nestock｜窩有')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            const Text('示範模式 · 資料不會儲存'),
            const Padding(
              padding: EdgeInsets.all(24),
              child: Icon(
                Icons.smart_toy_outlined,
                size: 72,
                semanticLabel: '窩有小幫手',
              ),
            ),
            Card(
              child: Padding(
                padding: const EdgeInsets.all(16),
                child: Text(_reply?.message ?? '歡迎回家！打開櫃子看看庫存，或告訴我你想整理什麼。'),
              ),
            ),
            const SizedBox(height: 16),
            Wrap(
              spacing: 12,
              runSpacing: 8,
              children: [
                OutlinedButton.icon(
                  onPressed: () => widget.openInventory(context),
                  icon: const Icon(Icons.kitchen_outlined),
                  label: const Text('打開庫存櫃'),
                ),
                OutlinedButton.icon(
                  onPressed: () => widget.openShopping(context),
                  icon: const Icon(Icons.shopping_cart_outlined),
                  label: const Text('打開購物清單'),
                ),
              ],
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _input,
              enabled: !_busy && approval == null,
              decoration: const InputDecoration(
                labelText: '告訴窩有…',
                border: OutlineInputBorder(),
              ),
              onSubmitted: (_) => _send(),
            ),
            Wrap(
              spacing: 12,
              children: [
                TextButton.icon(
                  onPressed: _openPhoto,
                  icon: const Icon(Icons.camera_alt_outlined),
                  label: const Text('拍照／照片'),
                ),
                FilledButton(
                  onPressed: _busy || approval != null ? null : _send,
                  child: const Text('送出'),
                ),
              ],
            ),
            if (_busy) const LoadingState(),
            if (_state == AgentState.error || _state == AgentState.timeout)
              ErrorState(
                timeout: _state == AgentState.timeout,
                onRetry: () => _perform(_retry!),
              ),
            for (final result in _reply?.results ?? <AgentResultView>[])
              AgentResultCard(result: result),
            if (approval != null)
              ApprovalCard(
                approval: approval,
                busy: _busy,
                onDecision: (approved) => _perform(
                  () => widget.client.resume(
                    approvalId: approval.id,
                    approved: approved,
                  ),
                ),
              ),
          ],
        ),
      ),
    );
  }
}
