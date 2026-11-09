import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart' show SystemUiOverlayStyle;

import '../../../shared/widgets/async_state.dart';
import '../domain/agent_client.dart';
import '../domain/agent_view_data.dart';
import 'agent_result_card.dart';
import 'approval_card.dart';
import 'confirmation_card.dart';
import 'agent_state.dart';
import 'home_scene.dart';
import '../../inventory/inventory_overlay.dart';
import '../../inventory/inventory_view_client.dart';
import 'agent_work_surface.dart';
import '../../shopping/shopping_overlay.dart';
import '../../shopping/shopping_view_client.dart';
import '../../photo/photo_client.dart';
import '../../photo/camera_capture.dart';
import '../../photo/photo_upload.dart';
import '../../speech/speech_client.dart';

/// Matches the opaque outer edge of the room artwork when it is letterboxed.
const _roomBackgroundBase = Color(0xFFEAEDF5);

/// The uploaded reference stays attached to its session-only photo message.
class PhotoMessageImage extends StatelessWidget {
  const PhotoMessageImage({
    super.key,
    required this.bytes,
    required this.imageRef,
  });
  final Uint8List bytes;
  final String imageRef;

  @override
  Widget build(BuildContext context) => Image.memory(
    bytes,
    key: const Key('message-photo'),
    width: 120,
    height: 120,
    fit: BoxFit.cover,
  );
}

class AgentHome extends StatefulWidget {
  const AgentHome({
    super.key,
    required this.client,
    required this.inventoryClient,
    required this.shoppingClient,
    this.photoClient = const UnavailablePhotoClient(),
    this.cameraCapture = const DeviceCameraCapture(),
    required this.photoUpload,
    required this.speechRecorder,
    required this.speechClient,
  });
  final AgentClient client;
  final PhotoClient photoClient;
  final CameraCapture cameraCapture;
  final PhotoUpload photoUpload;
  final SpeechRecorder speechRecorder;
  final SpeechClient speechClient;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;
  @override
  State<AgentHome> createState() => _AgentHomeState();
}

class _AgentHomeState extends State<AgentHome> {
  final _input = TextEditingController();
  Uint8List? _photo;
  bool _uploading = false;
  bool _speechBusy = false;
  bool _recording = false;
  bool _micHeld = false;
  Timer? _recordTimer;
  String? _uploadError;
  final _sceneScroll = ScrollController();
  final _interactionScroll = ScrollController();
  final List<
    ({
      bool user,
      String text,
      AgentReply? reply,
      Uint8List? photo,
      String? imageRef,
    })
  >
  _messages = [];
  int _conversationGeneration = 0;
  AgentReply? _reply;
  bool _workOpen = false;
  bool _inventoryOpen = false;
  bool _shoppingOpen = false;
  ConfirmationDraftFocus? _confirmationDraftFocus;
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
        _messages.add((
          user: false,
          text: reply.message,
          reply: reply,
          photo: null,
          imageRef: null,
        ));
        _reply = reply;
        _inventoryOpen = false;
        _shoppingOpen = false;
        _workOpen = true;
        _state = reply.approval == null
            ? AgentState.completed
            : AgentState.waitingForApproval;
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
      final photo = await widget.cameraCapture.capture();
      if (!mounted) return;
      if (photo != null) setState(() => _photo = photo);
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('無法開啟相機，請稍後再試。')));
      }
    }
  }

  Future<void> _startRecording() async {
    if (_speechBusy || _busy || _uploading) return;
    _micHeld = true;
    setState(() => _speechBusy = true);
    try {
      await widget.speechRecorder.start();
      if (!mounted) return;
      setState(() => _recording = true);
      if (!_micHeld) {
        await _stopRecording();
        return;
      }
      _recordTimer = Timer(const Duration(seconds: 30), () {
        _micHeld = false;
        _stopRecording();
      });
    } catch (_) {
      if (mounted) {
        setState(() {
          _speechBusy = false;
          _recording = false;
        });
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('無法開始錄音，請確認麥克風權限。')));
      }
    }
  }

  Future<void> _stopRecording() async {
    _micHeld = false;
    if (!_recording) return;
    _recordTimer?.cancel();
    setState(() => _recording = false);
    try {
      final bytes = await widget.speechRecorder.stop();
      if (bytes.isEmpty || bytes.length > 5 * 1024 * 1024) {
        throw StateError('invalid-size');
      }
      final transcript = (await widget.speechClient.transcribe(bytes)).trim();
      if (!mounted) return;
      if (transcript.isEmpty) throw StateError('empty');
      _applyTranscript(transcript);
    } catch (error) {
      if (mounted) {
        final message = switch (error) {
          SpeechHttpException(status: 400) => '錄音無效，請重試。',
          SpeechHttpException(status: 413) => '錄音檔過大，請重試。',
          SpeechHttpException(status: 415) => '不支援這種錄音格式。',
          SpeechHttpException(status: 502 || 504) => '語音服務暫時無法使用。',
          StateError(message: 'invalid-size') => '錄音為空或超過大小限制。',
          _ => '語音轉文字失敗或沒有辨識結果，請重試。',
        };
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text(message)));
      }
    } finally {
      if (mounted) setState(() => _speechBusy = false);
    }
  }

  void _applyTranscript(String transcript) {
    final draftFocus = _confirmationDraftFocus;
    if (draftFocus != null &&
        _workOpen &&
        !_inventoryOpen &&
        !_shoppingOpen &&
        draftFocus.isCurrent) {
      draftFocus.applyTranscript(transcript);
      return;
    }
    _confirmationDraftFocus = null;
    final current = _input.text;
    _input.text = current.isEmpty ? transcript : '$current $transcript';
    _input.selection = TextSelection.collapsed(offset: _input.text.length);
  }

  void _onConfirmationDraftFocusChanged(ConfirmationDraftFocus? draftFocus) {
    _confirmationDraftFocus = draftFocus;
  }

  void _closeInventory() {
    if (_inventoryOpen) setState(() => _inventoryOpen = false);
  }

  void _dismissRoomTransientUi() {
    FocusManager.instance.primaryFocus?.unfocus();
    if (_inventoryOpen || _shoppingOpen || _workOpen) {
      setState(() {
        _inventoryOpen = false;
        _shoppingOpen = false;
        _workOpen = false;
      });
    }
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

  Future<void> _send() async {
    final message = _input.text.trim();
    if ((message.isEmpty && _photo == null) ||
        _reply?.approval != null ||
        _busy ||
        _uploading ||
        _speechBusy) {
      return;
    }
    final photo = _photo;
    final generation = _conversationGeneration;
    String? imageRef;
    if (photo != null) {
      setState(() {
        _uploading = true;
        _uploadError = null;
      });
      try {
        imageRef = await widget.photoUpload.upload(photo);
      } catch (_) {
        if (mounted) {
          setState(() {
            _uploading = false;
            _uploadError = '照片上傳失敗，請重試。';
          });
        }
        return;
      }
      if (!mounted) return;
      if (generation != _conversationGeneration) {
        setState(() => _uploading = false);
        return;
      }
    }
    setState(() {
      _uploading = false;
      _messages.add((
        user: true,
        text: message,
        reply: null,
        photo: photo,
        imageRef: imageRef,
      ));
      _photo = null;
      _input.clear();
      _workOpen = true;
    });
    _showLatestMessage();
    _perform(
      () => imageRef != null && widget.client is PhotoAwareAgentClient
          ? (widget.client as PhotoAwareAgentClient).sendWithPhoto(
              message.isEmpty ? '照片' : message,
              imageRef,
            )
          : message.isEmpty
          ? Future.value(const AgentReply(message: '這是對話介面示範回覆。'))
          : widget.client.send(message),
    );
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
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        crossAxisAlignment: CrossAxisAlignment.end,
                        children: [
                          if (entry.photo != null)
                            PhotoMessageImage(
                              bytes: entry.photo!,
                              imageRef: entry.imageRef!,
                            ),
                          if (entry.text.isNotEmpty) Text(entry.text),
                        ],
                      ),
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
                    if (entry.reply!.mockConfirmationItems != null)
                      Padding(
                        padding: const EdgeInsets.fromLTRB(12, 8, 12, 16),
                        child: ConfirmationCard(
                          key: ValueKey('confirmation-${entry.reply}'),
                          items: entry.reply!.mockConfirmationItems!,
                          // Prototype controls intentionally have no side effects.
                          onCancel: () {},
                          onConfirm: () {},
                          onDraftFocusChanged: _onConfirmationDraftFocusChanged,
                        ),
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
    _recordTimer?.cancel();
    widget.speechRecorder.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final approval = _reply?.approval;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: const SystemUiOverlayStyle(
        statusBarColor: Colors.black,
        statusBarIconBrightness: Brightness.light,
        systemNavigationBarColor: Colors.black,
        systemNavigationBarIconBrightness: Brightness.light,
      ),
      child: SafeArea(
        key: const Key('home-safe-area'),
        child: Stack(
          key: const Key('home-viewport'),
          children: [
            const Positioned.fill(
              child: ColoredBox(
                key: Key('room-background-base'),
                color: _roomBackgroundBase,
              ),
            ),
            Positioned.fill(
              child: Image.asset(
                'assets/images/room/background.png',
                fit: BoxFit.contain,
                alignment: Alignment.center,
                excludeFromSemantics: true,
              ),
            ),
            Scaffold(
              backgroundColor: Colors.transparent,
              appBar: AppBar(
                title: const Text('Nestock｜窩有', style: TextStyle(fontSize: 18)),
                backgroundColor: Colors.transparent,
                surfaceTintColor: Colors.transparent,
              ),
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
                                        workSurfaceOpen:
                                            _workOpen &&
                                            !_inventoryOpen &&
                                            !_shoppingOpen,
                                        onBackgroundTap:
                                            _dismissRoomTransientUi,
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
                                        style: Theme.of(context)
                                            .textTheme
                                            .labelSmall,
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
                                        key: const Key('room-work-layer'),
                                        child: ClipRect(
                                          key: const Key('room-work-clip'),
                                          child: Align(
                                            alignment: Alignment.bottomCenter,
                                            child: ConstrainedBox(
                                              constraints: BoxConstraints(
                                                maxHeight:
                                                    constraints.maxHeight * .72,
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
                                                    child:
                                                        _interactionSurface(),
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
                                                maxHeight:
                                                    (constraints.maxHeight - 32)
                                                        .clamp(
                                                          0.0,
                                                          double.infinity,
                                                        ),
                                              ),
                                              child: AnimatedSwitcher(
                                                duration: const Duration(
                                                  milliseconds: 180,
                                                ),
                                                layoutBuilder:
                                                    (current, previous) =>
                                                        Stack(
                                                          alignment: Alignment
                                                              .topRight,
                                                          children: [
                                                            ...previous,
                                                            ?current,
                                                          ],
                                                        ),
                                                transitionBuilder:
                                                    (
                                                      child,
                                                      animation,
                                                    ) => FadeTransition(
                                                      opacity: _inventoryOpen
                                                          ? const AlwaysStoppedAnimation(
                                                              0,
                                                            )
                                                          : animation,
                                                      child: ScaleTransition(
                                                        alignment:
                                                            RoomAnchors.cart,
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
                                                        client: widget
                                                            .shoppingClient,
                                                        onClose: _closeShopping,
                                                      )
                                                    : const SizedBox.shrink(
                                                        key: Key(
                                                          'shopping-hidden',
                                                        ),
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
                                                maxHeight:
                                                    (constraints.maxHeight - 32)
                                                        .clamp(
                                                          0.0,
                                                          double.infinity,
                                                        ),
                                              ),
                                              child: AnimatedSwitcher(
                                                duration: const Duration(
                                                  milliseconds: 180,
                                                ),
                                                layoutBuilder:
                                                    (current, previous) =>
                                                        Stack(
                                                          alignment:
                                                              Alignment.topLeft,
                                                          children: [
                                                            ...previous,
                                                            ?current,
                                                          ],
                                                        ),
                                                transitionBuilder:
                                                    (
                                                      child,
                                                      animation,
                                                    ) => FadeTransition(
                                                      opacity: _shoppingOpen
                                                          ? const AlwaysStoppedAnimation(
                                                              0,
                                                            )
                                                          : animation,
                                                      child: ScaleTransition(
                                                        alignment:
                                                            RoomAnchors.cabinet,
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
                                                        client: widget
                                                            .inventoryClient,
                                                        onClose:
                                                            _closeInventory,
                                                      )
                                                    : const SizedBox.shrink(
                                                        key: Key(
                                                          'inventory-hidden',
                                                        ),
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
                    if (_uploading) const LinearProgressIndicator(),
                    if (_uploadError != null) Text(_uploadError!),
                    if (_state == AgentState.error ||
                        _state == AgentState.timeout)
                      ErrorState(
                        timeout: _state == AgentState.timeout,
                        onRetry: () => _perform(_retry!),
                      ),
                    Padding(
                      key: const Key('composer'),
                      padding: const EdgeInsets.symmetric(
                        horizontal: 8,
                        vertical: 4,
                      ),
                      child: Column(
                        mainAxisSize: MainAxisSize.min,
                        children: [
                          if (_photo != null)
                            Align(
                              alignment: Alignment.centerLeft,
                              child: Stack(
                                children: [
                                  Padding(
                                    padding: const EdgeInsets.all(4),
                                    child: Image.memory(
                                      _photo!,
                                      key: const Key('photo-attachment'),
                                      width: 64,
                                      height: 64,
                                      fit: BoxFit.cover,
                                    ),
                                  ),
                                  Positioned(
                                    right: 0,
                                    top: 0,
                                    child: IconButton(
                                      tooltip: '移除照片',
                                      onPressed: _uploading
                                          ? null
                                          : () => setState(() => _photo = null),
                                      icon: const Icon(Icons.cancel),
                                      visualDensity: VisualDensity.compact,
                                    ),
                                  ),
                                ],
                              ),
                            ),
                          Row(
                            children: [
                              Expanded(
                                child: TextField(
                                  controller: _input,
                                  onTap: () {
                                    _closeInventory();
                                    _closeShopping();
                                  },
                                  enabled:
                                      !_busy && !_uploading && approval == null,
                                  decoration: const InputDecoration(
                                    hintText: '告訴Noki...',
                                    border: OutlineInputBorder(),
                                    isDense: true,
                                  ),
                                  onSubmitted: (_) => _send(),
                                ),
                              ),
                              GestureDetector(
                                onTapDown: (_) => _startRecording(),
                                onTapUp: (_) => _stopRecording(),
                                onTapCancel: _stopRecording,
                                child: Tooltip(
                                  message: '語音輸入',
                                  child: Padding(
                                    padding: const EdgeInsets.all(12),
                                    child: Icon(
                                      _recording ? Icons.mic : Icons.mic_none,
                                    ),
                                  ),
                                ),
                              ),
                              FilledButton(
                                onPressed:
                                    _busy ||
                                        _uploading ||
                                        _speechBusy ||
                                        approval != null
                                    ? null
                                    : _send,
                                child: const Text('送出'),
                              ),
                            ],
                          ),
                        ],
                      ),
                    ),
                    Row(
                      key: const Key('toolbar'),
                      mainAxisAlignment: MainAxisAlignment.spaceEvenly,
                      children: [
                        TextButton.icon(
                          onPressed: _uploading ? null : _openPhoto,
                          icon: const Icon(Icons.camera_alt_outlined),
                          label: const Text('拍照'),
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
            ),
          ],
        ),
      ),
    );
  }
}
