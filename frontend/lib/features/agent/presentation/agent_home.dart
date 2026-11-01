import 'dart:async';

import 'package:flutter/material.dart';

import '../../../shared/widgets/async_state.dart';
import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import 'agent_result_card.dart';
import 'approval_card.dart';
import 'agent_state.dart';
import 'home_scene.dart';
import '../../inventory/inventory_overlay.dart';
import '../../inventory/inventory_view_client.dart';
import 'agent_work_surface.dart';
import '../../shopping/shopping_overlay.dart';
import '../../shopping/shopping_view_client.dart';
import '../../photo/photo_client.dart';

class AgentHome extends StatefulWidget {
  const AgentHome({
    super.key,
    required this.client,
    required this.inventoryClient,
    required this.shoppingClient,
    this.photoClient = const UnavailablePhotoClient(),
  });
  final AgentClient client;
  final PhotoClient photoClient;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;
  @override
  State<AgentHome> createState() => _AgentHomeState();
}

class _AgentHomeState extends State<AgentHome> {
  final _input = TextEditingController();
  final _sceneScroll = ScrollController();
  final _interactionScroll = ScrollController();
  final List<({bool user, String text, AgentReply? reply})> _messages = [];
  int _conversationGeneration = 0;
  AgentReply? _reply;
  bool _workOpen = false;
  bool _inventoryOpen = false;
  bool _shoppingOpen = false;
  AgentState _state = AgentState.idle;
  bool get _busy => _state == AgentState.loading;
  Future<AgentReply> Function()? _retry;

  Future<void> _perform(Future<AgentReply> Function() action) async {
    if (_busy) return;
    final generation = _conversationGeneration;
    setState(() {
      _state = AgentState.loading;
      _retry = action;
    });
    try {
      final reply = await action();
      if (!mounted || generation != _conversationGeneration) return;
      setState(() {
        _messages.add((user: false, text: reply.message, reply: reply));
        _reply = reply;
        _inventoryOpen = false;
        _shoppingOpen = false;
        _workOpen = true;
        _state = reply.approval == null
            ? AgentState.completed
            : AgentState.waitingForApproval;
        _input.clear();
      });
      if (reply.mockVisionItems == null) _showLatestMessage();
    } on TimeoutException {
      if (mounted && generation == _conversationGeneration) {
        setState(() => _state = AgentState.timeout);
      }
    } catch (_) {
      if (mounted && generation == _conversationGeneration) {
        setState(() => _state = AgentState.error);
      }
    }
  }

  void _clearConversation() {
    setState(() {
      _conversationGeneration++;
      _messages.clear();
      _workOpen = false;
      _input.clear();
      _state = AgentState.idle;
      _retry = null;
      _reply = null;
    });
  }

  Future<void> _openPhoto() async {
    _closeInventory();
    _closeShopping();
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

  void _closeInventory() {
    if (_inventoryOpen) setState(() => _inventoryOpen = false);
  }

  void _closeShopping() {
    if (_shoppingOpen) setState(() => _shoppingOpen = false);
  }

  void _openShopping() {
    setState(() {
      _inventoryOpen = false;
      _shoppingOpen = !_shoppingOpen;
    });
    if (_sceneScroll.hasClients) {
      _sceneScroll.animateTo(
        0,
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
      );
    }
  }

  void _openInventory() {
    setState(() {
      _shoppingOpen = false;
      _inventoryOpen = !_inventoryOpen;
    });
    if (_sceneScroll.hasClients) {
      _sceneScroll.animateTo(
        0,
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
      );
    }
  }

  void _send() {
    final message = _input.text.trim();
    if (message.isEmpty || _reply?.approval != null) return;
    setState(() {
      _messages.add((user: true, text: message, reply: null));
      _workOpen = true;
    });
    _showLatestMessage();
    _perform(() => widget.client.send(message));
  }

  void _showLatestMessage() {
    WidgetsBinding.instance.addPostFrameCallback((_) {
      if (!mounted || !_interactionScroll.hasClients) return;
      _interactionScroll.animateTo(
        _interactionScroll.position.maxScrollExtent,
        duration: const Duration(milliseconds: 180),
        curve: Curves.easeOut,
      );
    });
  }

  Widget _interactionSurface() => Material(
    key: const Key('work-surface'),
    color: Theme.of(context).colorScheme.surface,
    borderRadius: const BorderRadius.vertical(top: Radius.circular(20)),
    child: Column(
      mainAxisSize: MainAxisSize.min,
      children: [
        Row(
          children: [
            TextButton(
              onPressed: _clearConversation,
              child: const Text('清除對話'),
            ),
            const Spacer(),
            IconButton(
              onPressed: () => setState(() => _workOpen = false),
              icon: const Icon(Icons.close),
              tooltip: '關閉',
            ),
          ],
        ),
        Flexible(
          child: SingleChildScrollView(
            key: const Key('work-items'),
            controller: _interactionScroll,
            child: Column(
              children: [
                for (final entry in _messages) ...[
                  Align(
                    alignment: entry.user
                        ? Alignment.centerRight
                        : Alignment.centerLeft,
                    child: Container(
                      margin: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 4,
                      ),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 12,
                        vertical: 8,
                      ),
                      decoration: BoxDecoration(
                        color: entry.user
                            ? Theme.of(context).colorScheme.primaryContainer
                            : Theme.of(context)
                                  .colorScheme
                                  .surfaceContainerHighest,
                        borderRadius: BorderRadius.circular(14),
                      ),
                      child: Text(entry.text),
                    ),
                  ),
                  if (entry.reply != null) ...[
                    for (final result in entry.reply!.results)
                      AgentResultCard(result: result),
                    if (entry.reply!.approval != null &&
                        identical(entry.reply, _reply))
                      ApprovalCard(
                        approval: entry.reply!.approval!,
                        busy: _busy,
                        onDecision: (approved) => _perform(
                          () => widget.client.resume(
                            approvalId: entry.reply!.approval!.id,
                            approved: approved,
                          ),
                        ),
                      ),
                    if (entry.reply!.mockVisionItems != null)
                      AgentWorkSurface(
                        key: ValueKey(entry.reply),
                        items: entry.reply!.mockVisionItems!,
                        embedded: true,
                        onClose: () => setState(() => _workOpen = false),
                      ),
                  ],
                ],
              ],
            ),
          ),
        ),
      ],
    ),
  );

  @override
  void dispose() {
    _input.dispose();
    _sceneScroll.dispose();
    _interactionScroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final approval = _reply?.approval;
    return Scaffold(
      appBar: AppBar(title: const Text('Nestock｜窩有')),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child: LayoutBuilder(
                builder: (context, constraints) => SingleChildScrollView(
                  key: const Key('scene-scroll'),
                  controller: _sceneScroll,
                  child: Column(
                    children: [
                      SizedBox(
                        height: constraints.maxHeight,
                        child: Stack(
                          children: [
                            Positioned.fill(
                              child: HomeScene(
                                onInventory: () => _openInventory(),
                                onShopping: () {
                                  _openShopping();
                                },
                                message: _busy ? '思考中...' : '歡迎回家',
                              ),
                            ),
                            Positioned(
                              left: 8,
                              bottom: 4,
                              child: Text(
                                '示範模式 · 資料不會儲存',
                                style: Theme.of(context).textTheme.labelSmall,
                              ),
                            ),
                            if (_messages.isNotEmpty &&
                                !_workOpen &&
                                !_inventoryOpen &&
                                !_shoppingOpen)
                              Positioned(
                                right: 8,
                                bottom: 4,
                                child: TextButton(
                                  onPressed: () =>
                                      setState(() => _workOpen = true),
                                  child: const Text('顯示互動'),
                                ),
                              ),
                            if (_messages.isNotEmpty)
                              Positioned.fill(
                                child: ClipRect(
                                  key: const Key('room-work-clip'),
                                  child: Align(
                                    alignment: Alignment.bottomCenter,
                                    child: ConstrainedBox(
                                      constraints: BoxConstraints(
                                        maxHeight: constraints.maxHeight * .72,
                                      ),
                                      child: IgnorePointer(
                                        ignoring:
                                            !_workOpen ||
                                            _inventoryOpen ||
                                            _shoppingOpen,
                                        child: AnimatedSlide(
                                          offset: _workOpen
                                              ? Offset.zero
                                              : const Offset(0, 1),
                                          duration: const Duration(
                                            milliseconds: 220,
                                          ),
                                          child: AnimatedOpacity(
                                            opacity:
                                                _workOpen &&
                                                    !_inventoryOpen &&
                                                    !_shoppingOpen
                                                ? 1
                                                : 0,
                                            duration: const Duration(
                                              milliseconds: 220,
                                            ),
                                            child: _interactionSurface(),
                                          ),
                                        ),
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            Positioned.fill(
                              child: ClipRect(
                                key: const Key('shopping-room-clip'),
                                child: Padding(
                                  padding: const EdgeInsets.all(16),
                                  child: Align(
                                    alignment: Alignment.topRight,
                                    child: ConstrainedBox(
                                      constraints: BoxConstraints(
                                        maxHeight: constraints.maxHeight - 32,
                                      ),
                                      child: AnimatedSwitcher(
                                        duration: const Duration(
                                          milliseconds: 180,
                                        ),
                                        layoutBuilder: (current, previous) =>
                                            Stack(
                                              alignment: Alignment.topRight,
                                              children: [...previous, ?current],
                                            ),
                                        transitionBuilder: (child, animation) =>
                                            FadeTransition(
                                              opacity: _inventoryOpen
                                                  ? const AlwaysStoppedAnimation(
                                                      0,
                                                    )
                                                  : animation,
                                              child: ScaleTransition(
                                                alignment: RoomAnchors.cart,
                                                scale: Tween<double>(
                                                  begin: .94,
                                                  end: 1,
                                                ).animate(animation),
                                                child: child,
                                              ),
                                            ),
                                        child: _shoppingOpen
                                            ? ShoppingOverlay(
                                                key: const Key(
                                                  'shopping-panel',
                                                ),
                                                client: widget.shoppingClient,
                                                onClose: _closeShopping,
                                              )
                                            : const SizedBox.shrink(
                                                key: Key('shopping-hidden'),
                                              ),
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ),
                            Positioned.fill(
                              child: ClipRect(
                                key: const Key('inventory-room-clip'),
                                child: Padding(
                                  padding: const EdgeInsets.all(16),
                                  child: Align(
                                    alignment: Alignment.topLeft,
                                    child: ConstrainedBox(
                                      constraints: BoxConstraints(
                                        maxHeight: constraints.maxHeight - 32,
                                      ),
                                      child: AnimatedSwitcher(
                                        duration: const Duration(
                                          milliseconds: 180,
                                        ),
                                        layoutBuilder: (current, previous) =>
                                            Stack(
                                              alignment: Alignment.topLeft,
                                              children: [...previous, ?current],
                                            ),
                                        transitionBuilder: (child, animation) =>
                                            FadeTransition(
                                              opacity: _shoppingOpen
                                                  ? const AlwaysStoppedAnimation(
                                                      0,
                                                    )
                                                  : animation,
                                              child: ScaleTransition(
                                                alignment: RoomAnchors.cabinet,
                                                scale: Tween<double>(
                                                  begin: .94,
                                                  end: 1,
                                                ).animate(animation),
                                                child: child,
                                              ),
                                            ),
                                        child: _inventoryOpen
                                            ? InventoryOverlay(
                                                key: const Key(
                                                  'inventory-panel',
                                                ),
                                                client: widget.inventoryClient,
                                                onClose: _closeInventory,
                                              )
                                            : const SizedBox.shrink(
                                                key: Key('inventory-hidden'),
                                              ),
                                      ),
                                    ),
                                  ),
                                ),
                              ),
                            ),
                          ],
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            ),
            if (_busy) const LoadingState(),
            if (_state == AgentState.error || _state == AgentState.timeout)
              ErrorState(
                timeout: _state == AgentState.timeout,
                onRetry: () => _perform(_retry!),
              ),
            Padding(
              key: const Key('composer'),
              padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 4),
              child: Row(
                children: [
                  Expanded(
                    child: TextField(
                      controller: _input,
                      onTap: () {
                        _closeInventory();
                        _closeShopping();
                      },
                      enabled: !_busy && approval == null,
                      decoration: const InputDecoration(
                        labelText: '告訴窩有…',
                        border: OutlineInputBorder(),
                        isDense: true,
                      ),
                      onSubmitted: (_) => _send(),
                    ),
                  ),
                  IconButton(
                    onPressed: () => ScaffoldMessenger.of(
                      context,
                    ).showSnackBar(const SnackBar(content: Text('語音輸入尚未開放。'))),
                    icon: const Icon(Icons.mic_none),
                    tooltip: '語音輸入',
                  ),
                  FilledButton(
                    onPressed: _busy || approval != null ? null : _send,
                    child: const Text('送出'),
                  ),
                ],
              ),
            ),
            Row(
              key: const Key('toolbar'),
              mainAxisAlignment: MainAxisAlignment.spaceEvenly,
              children: [
                TextButton.icon(
                  onPressed: _openPhoto,
                  icon: const Icon(Icons.camera_alt_outlined),
                  label: const Text('拍照／照片'),
                ),
                TextButton(
                  onPressed: () => _openInventory(),
                  child: const Text('庫存'),
                ),
                TextButton(
                  onPressed: () {
                    _openShopping();
                  },
                  child: const Text('購物清單'),
                ),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
