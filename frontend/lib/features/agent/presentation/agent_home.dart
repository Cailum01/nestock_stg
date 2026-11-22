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
import '../../shopping/commerce_options_client.dart';
import '../../photo/photo_client.dart';
import '../../photo/camera_capture.dart';
import '../../photo/photo_upload.dart';
import '../../speech/speech_client.dart';

/// Matches the opaque outer edge of the room artwork when it is letterboxed.
const _roomBackgroundBase = Color(0xFFEAEDF5);
const _composerFadeHeight = 96.0;
const _composerPanelHeight = 144.0;

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
    required this.commerceOptionsClient,
    this.photoClient = const UnavailablePhotoClient(),
    this.cameraCapture = const DeviceCameraCapture(),
    required this.photoUpload,
    required this.speechRecorder,
    required this.speechClient,
    this.appBarActions = const [],
    this.onConnectHousehold,
  });
  final AgentClient client;
  final PhotoClient photoClient;
  final CameraCapture cameraCapture;
  final PhotoUpload photoUpload;
  final SpeechRecorder speechRecorder;
  final SpeechClient speechClient;
  final InventoryViewClient inventoryClient;
  final ShoppingViewClient shoppingClient;
  final CommerceOptionsClient commerceOptionsClient;
  final List<Widget> appBarActions;
  final VoidCallback? onConnectHousehold;
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
  Future<void> Function()? _retry;

  Future<void> _perform(Future<AgentReply> Function() action) async {
    if (_busy) return;
    final generation = _conversationGeneration;
    setState(() {
      _state = AgentState.loading;
      _retry = () => _perform(action);
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

  Future<void> _submitCardChanges({
    required AgentReply currentReply,
    required List<CardChange> changes,
  }) async {
    final approval = currentReply.approval;
    if (_busy || approval == null) return;
    final generation = _conversationGeneration;
    setState(() {
      _state = AgentState.loading;
      _retry = () =>
          _submitCardChanges(currentReply: currentReply, changes: changes);
    });
    try {
      final client = widget.client;
      final householdId = approval.householdId;
      final reply = client is HouseholdBoundAgentClient && householdId != null
          ? await (client as HouseholdBoundAgentClient)
                .submitCardChangesInHousehold(
                  approvalId: approval.id,
                  changes: changes,
                  householdId: householdId,
                )
          : await client.submitCardChanges(
              approvalId: approval.id,
              changes: changes,
            );
      if (!mounted || generation != _conversationGeneration) return;
      final code = _cardChangeResultCode(reply);
      final returnedCard = reply.approval;
      final returnsOriginalCard =
          returnedCard != null && returnedCard.id == approval.id;
      final returnsReplacementCard =
          returnedCard != null && returnedCard.id != approval.id;
      final terminalNoCard =
          returnedCard == null && code == 'card_not_changeable';
      setState(() {
        _messages.add((
          user: false,
          text: reply.message,
          reply: reply,
          photo: null,
          imageRef: null,
        ));
        // A received card-change response is authoritative for which card is
        // currently actionable. A refusal that includes the original card
        // keeps it on screen; card_not_changeable deliberately includes none.
        // Do not make this change for a timeout/network failure below: there
        // was no authoritative response in those cases.
        if (returnsReplacementCard || returnsOriginalCard || terminalNoCard) {
          _reply = reply;
        } else if (returnedCard == null) {
          // A response with no pending card can never leave a local action
          // active, even if an unknown future backend code accompanies it.
          _reply = reply;
        }
        _workOpen = true;
        _state = reply.approval == null
            ? AgentState.completed
            : AgentState.waitingForApproval;
      });
      _showLatestMessage();
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

  String? _cardChangeResultCode(AgentReply reply) {
    for (final result in reply.results) {
      final code = result.data['code'];
      if (code is String && code.isNotEmpty) return code;
    }
    return null;
  }

  Map<String, String> _cardChangeErrors(AgentReply reply) {
    final errors = <String, String>{};
    for (final result in reply.results) {
      final data = result.data;
      if (data['code'] != 'line_refused' || data['lines'] is! List) continue;
      for (final line in data['lines'] as List<dynamic>) {
        if (line is! Map) continue;
        final operationId = line['operation_id'];
        final reason = line['reason'];
        if (operationId is String &&
            operationId.isNotEmpty &&
            reason is String) {
          errors[operationId] = reason;
        }
      }
    }
    return errors;
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

  Widget _clusterActionButton({
    required Key key,
    required String tooltip,
    required IconData icon,
    required VoidCallback? onPressed,
  }) => Tooltip(
    message: tooltip,
    child: Semantics(
      button: true,
      enabled: onPressed != null,
      label: tooltip,
      child: SizedBox(
        key: key,
        width: 48,
        height: 48,
        child: Material(
          color: const Color(0xFFC2CDEE),
          shape: const CircleBorder(),
          clipBehavior: Clip.antiAlias,
          child: InkWell(
            onTap: onPressed,
            customBorder: const CircleBorder(),
            child: Icon(icon, color: const Color(0xFF252D4A)),
          ),
        ),
      ),
    ),
  );

  Widget _voiceClusterButton({required bool enabled}) => Tooltip(
    message: '語音輸入',
    triggerMode: TooltipTriggerMode.manual,
    child: Semantics(
      button: true,
      enabled: enabled,
      label: '語音輸入',
      child: SizedBox(
        key: const Key('paw-voice'),
        width: 48,
        height: 48,
        child: Material(
          color: enabled ? const Color(0xFFC2CDEE) : const Color(0xFFD9DEF0),
          shape: const CircleBorder(),
          clipBehavior: Clip.antiAlias,
          child: GestureDetector(
            behavior: HitTestBehavior.opaque,
            // Keep these recognizers installed while recording. Removing them
            // during the state update from _startRecording would cancel the
            // active gesture while this widget is rebuilding.
            onTapDown: (_) => _startRecording(),
            onTapUp: (_) => _stopRecording(),
            onTapCancel: _stopRecording,
            child: Icon(
              _recording ? Icons.mic : Icons.mic_none,
              color: const Color(0xFF252D4A),
            ),
          ),
        ),
      ),
    ),
  );

  Widget _composerInputArea({required bool enabled}) {
    final textField = TextField(
      controller: _input,
      onTap: () {
        _closeInventory();
        _closeShopping();
      },
      enabled: enabled,
      decoration: const InputDecoration(
        hintText: '告訴Noki...',
        border: OutlineInputBorder(),
        isDense: true,
      ),
      onSubmitted: (_) => _send(),
    );
    final photo = _photo;
    if (photo == null) return textField;

    return DecoratedBox(
      key: const Key('composer-attachment-container'),
      decoration: BoxDecoration(
        color: const Color(0xFFFFFCF6),
        border: Border.all(color: const Color(0xFF40599C)),
        borderRadius: BorderRadius.circular(18),
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.fromLTRB(6, 6, 4, 4),
            child: Row(
              children: [
                Stack(
                  clipBehavior: Clip.none,
                  children: [
                    ClipRRect(
                      borderRadius: BorderRadius.circular(10),
                      child: Image.memory(
                        photo,
                        key: const Key('photo-attachment'),
                        width: 56,
                        height: 56,
                        fit: BoxFit.cover,
                      ),
                    ),
                    Positioned(
                      right: -5,
                      top: -5,
                      child: Tooltip(
                        message: '移除照片',
                        child: Semantics(
                          button: true,
                          label: '移除照片',
                          child: SizedBox(
                            width: 28,
                            height: 28,
                            child: Material(
                              color: const Color(0xFF40599C),
                              shape: const CircleBorder(),
                              clipBehavior: Clip.antiAlias,
                              child: InkWell(
                                onTap: _uploading
                                    ? null
                                    : () => setState(() => _photo = null),
                                customBorder: const CircleBorder(),
                                child: const Icon(
                                  Icons.close,
                                  size: 18,
                                  color: Colors.white,
                                ),
                              ),
                            ),
                          ),
                        ),
                      ),
                    ),
                  ],
                ),
                const SizedBox(width: 10),
                const Expanded(
                  child: Text(
                    '已附加至下一則訊息',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(color: Color(0xFF252D4A), fontSize: 12),
                  ),
                ),
              ],
            ),
          ),
          const Divider(height: 1, color: Color(0xFFBBC4D8)),
          SizedBox(
            height: 52,
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 8),
              child: TextField(
                controller: _input,
                onTap: () {
                  _closeInventory();
                  _closeShopping();
                },
                enabled: enabled,
                decoration: const InputDecoration(
                  hintText: '告訴Noki...',
                  border: InputBorder.none,
                  isDense: true,
                ),
                onSubmitted: (_) => _send(),
              ),
            ),
          ),
        ],
      ),
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

  Iterable<AgentResultView> _visibleResults(AgentReply reply) {
    final approval = reply.approval;
    if (!_isPhotoCreateItemApproval(approval)) return reply.results;

    final approvalNames = approval!.operations
        .map((operation) => _displayName(operation.values['name']))
        .whereType<String>()
        .toSet();
    if (approvalNames.isEmpty) return reply.results;

    return reply.results.where(
      (result) => !_isRedundantPhotoVisionResult(result, approvalNames),
    );
  }

  bool _isPhotoCreateItemApproval(ApprovalView? approval) =>
      approval != null &&
      approval.operations.isNotEmpty &&
      approval.operations.every(
        (operation) =>
            operation.action == 'create_item' &&
            !_hasShoppingItemId(operation.values['shopping_item_id']),
      );

  bool _isRedundantPhotoVisionResult(
    AgentResultView result,
    Set<String> approvalNames,
  ) {
    if (result.kind != ResultKind.vision) return false;
    final rawItems = result.data['items'];
    if (rawItems is! List || rawItems.isEmpty) return false;
    final visionNames = rawItems
        .map(
          (item) => item is Map
              ? _displayName(item['name']) ?? _displayName(item['seen_as'])
              : null,
        )
        .toList(growable: false);
    return visionNames.isNotEmpty &&
        visionNames.every(
          (name) => name != null && approvalNames.contains(name),
        );
  }

  String? _displayName(Object? value) =>
      value is String && value.trim().isNotEmpty ? value.trim() : null;

  bool _hasShoppingItemId(Object? value) =>
      value is String && value.trim().isNotEmpty;

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
                    for (final result in _visibleResults(entry.reply!))
                      AgentResultCard(result: result),
                    if (entry.reply!.approval != null &&
                        identical(entry.reply, _reply))
                      ApprovalCard(
                        key: ValueKey('approval-${entry.reply!.approval!.id}'),
                        approval: entry.reply!.approval!,
                        busy: _busy,
                        changeErrors: _cardChangeErrors(entry.reply!),
                        onDecision:
                            ({
                              required approved,
                              selectedOptionId,
                              approvedOperationIds,
                            }) => _perform(() {
                              final approval = entry.reply!.approval!;
                              final client = widget.client;
                              return client is HouseholdBoundAgentClient &&
                                      approval.householdId != null
                                  ? (client as HouseholdBoundAgentClient)
                                        .resumeInHousehold(
                                          approvalId: approval.id,
                                          approved: approved,
                                          householdId: approval.householdId!,
                                          selectedOptionId: selectedOptionId,
                                          approvedOperationIds:
                                              approvedOperationIds,
                                        )
                                  : client.resume(
                                      approvalId: approval.id,
                                      approved: approved,
                                      selectedOptionId: selectedOptionId,
                                      approvedOperationIds:
                                          approvedOperationIds,
                                    );
                            }),
                        onSubmitCardChanges: (changes) => _submitCardChanges(
                          currentReply: entry.reply!,
                          changes: changes,
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
                title: widget.onConnectHousehold == null
                    ? const Text('Nestock｜窩有', style: TextStyle(fontSize: 18))
                    : Semantics(
                        button: true,
                        label: 'Connect a household',
                        child: InkWell(
                          key: const Key('home-brand-connect'),
                          borderRadius: BorderRadius.circular(8),
                          onTap: widget.onConnectHousehold,
                          child: const Padding(
                            padding: EdgeInsets.symmetric(
                              horizontal: 8,
                              vertical: 6,
                            ),
                            child: Row(
                              mainAxisSize: MainAxisSize.min,
                              children: [
                                Icon(Icons.home_outlined, size: 20),
                                SizedBox(width: 8),
                                Text(
                                  'Nestock｜窩有',
                                  style: TextStyle(fontSize: 18),
                                ),
                              ],
                            ),
                          ),
                        ),
                      ),
                backgroundColor: Colors.transparent,
                surfaceTintColor: Colors.transparent,
                actions: widget.appBarActions,
              ),
              body: SafeArea(
                child: Column(
                  key: const Key('home-layout-column'),
                  children: [
                    Expanded(
                      key: const Key('scene-layer'),
                      child: LayoutBuilder(
                        builder: (context, constraints) => SingleChildScrollView(
                          key: const Key('scene-scroll'),
                          controller: _sceneScroll,
                          clipBehavior: Clip.none,
                          child: Column(
                            children: [
                              SizedBox(
                                height: constraints.maxHeight,
                                child: Stack(
                                  clipBehavior: Clip.none,
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
                                        statusMessage: _busy
                                            ? '思考中...'
                                            : _workOpen
                                            ? '歡迎回家'
                                            : null,
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
                                                        commerceClient: widget
                                                            .commerceOptionsClient,
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
                        onRetry: _retry!,
                      ),
                    Padding(
                      key: const Key('composer'),
                      padding: const EdgeInsets.symmetric(horizontal: 8),
                      child: Stack(
                        clipBehavior: Clip.none,
                        children: [
                          Positioned(
                            key: const Key('composer-background-fade-layer'),
                            left: -8,
                            right: -8,
                            bottom: 0,
                            height: _composerPanelHeight + _composerFadeHeight,
                            child: const IgnorePointer(
                              child: DecoratedBox(
                                key: Key('composer-background-fade'),
                                decoration: BoxDecoration(
                                  gradient: LinearGradient(
                                    begin: Alignment.topCenter,
                                    end: Alignment.bottomCenter,
                                    colors: [
                                      Color(0x00EAEDF5),
                                      Color(0xB3EAEDF5),
                                      _roomBackgroundBase,
                                    ],
                                    stops: [0, .62, 1],
                                  ),
                                ),
                              ),
                            ),
                          ),
                          Column(
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Padding(
                                padding: const EdgeInsets.only(bottom: 16),
                                child: SizedBox(
                                  key: const Key('paw-action-cluster'),
                                  height: 128,
                                  child: Stack(
                                    children: [
                                      Positioned(
                                        left: 0,
                                        right: 132,
                                        bottom: 0,
                                        top: _photo == null ? null : 0,
                                        child: _composerInputArea(
                                          enabled:
                                              !_busy &&
                                              !_uploading &&
                                              approval == null,
                                        ),
                                      ),
                                      Positioned(
                                        right: 0,
                                        top: 7,
                                        child: _clusterActionButton(
                                          key: const Key('paw-camera'),
                                          tooltip: '拍照',
                                          icon: Icons.camera_alt_outlined,
                                          onPressed: _uploading
                                              ? null
                                              : _openPhoto,
                                        ),
                                      ),
                                      Positioned(
                                        right: 56,
                                        top: 23,
                                        child: _voiceClusterButton(
                                          enabled:
                                              !_speechBusy &&
                                              !_busy &&
                                              !_uploading,
                                        ),
                                      ),
                                      Positioned(
                                        right: 74,
                                        bottom: 4,
                                        child: _clusterActionButton(
                                          key: const Key(
                                            'paw-expand-interaction',
                                          ),
                                          tooltip: _workOpen ? '收合對話' : '展開對話',
                                          icon: _workOpen
                                              ? Icons.expand_more
                                              : Icons.expand_less,
                                          onPressed:
                                              _messages.isEmpty ||
                                                  _inventoryOpen ||
                                                  _shoppingOpen
                                              ? null
                                              : () => setState(
                                                  () => _workOpen = !_workOpen,
                                                ),
                                        ),
                                      ),
                                      Positioned(
                                        right: 0,
                                        bottom: 0,
                                        child: Tooltip(
                                          message: '送出',
                                          child: SizedBox(
                                            key: const Key('paw-send'),
                                            width: 64,
                                            height: 64,
                                            child: FilledButton(
                                              onPressed:
                                                  _busy ||
                                                      _uploading ||
                                                      _speechBusy ||
                                                      approval != null
                                                  ? null
                                                  : _send,
                                              style: FilledButton.styleFrom(
                                                backgroundColor: const Color(
                                                  0xFF40599C,
                                                ),
                                                foregroundColor: Colors.white,
                                                shape: const CircleBorder(),
                                                padding: EdgeInsets.zero,
                                              ),
                                              child: const Icon(Icons.send),
                                            ),
                                          ),
                                        ),
                                      ),
                                    ],
                                  ),
                                ),
                              ),
                            ],
                          ),
                        ],
                      ),
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
