import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Scene geometry and overlay origin live together so artwork can replace props.
abstract final class RoomAnchors {
  static const cabinet = Alignment(-.86, -.88);
  static const cart = Alignment(.87, .15);
}

/// The placeholder can later be replaced with a weather image within the frame.
class SceneWindow extends StatelessWidget {
  const SceneWindow({super.key, this.weather = const SizedBox.expand()});
  final Widget weather;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) => Stack(
      children: [
        Positioned(
          left: constraints.maxWidth * .15,
          top: constraints.maxHeight * .30,
          width: constraints.maxWidth * .70,
          height: constraints.maxHeight * .55,
          child: ClipRect(key: const Key('weather-viewport'), child: weather),
        ),
        Positioned.fill(
          child: Image.asset(
            'assets/images/objects/window.png',
            key: const Key('window-image'),
            fit: BoxFit.contain,
            excludeFromSemantics: true,
          ),
        ),
      ],
    ),
  );
}

/// Keeps the idle character visually alive without changing its scene bounds.
///
/// The controller runs one breathing cycle at a time, with a brief pause at the
/// resting pose so the motion reads as a calm breath instead of a pulse.
class _IdleNoki extends StatefulWidget {
  const _IdleNoki({required this.child});

  final Widget child;

  @override
  State<_IdleNoki> createState() => _IdleNokiState();
}

class _IdleNokiState extends State<_IdleNoki>
    with SingleTickerProviderStateMixin {
  static const _cycleDuration = Duration(milliseconds: 2800);
  static const _initialDelay = Duration(seconds: 1);
  static const _betweenCycles = Duration(milliseconds: 200);

  late final AnimationController _controller;
  Timer? _nextCycle;

  @override
  void initState() {
    super.initState();
    _controller = AnimationController(duration: _cycleDuration, vsync: this)
      ..addStatusListener(_scheduleFollowingCycle);
    _startAfter(_initialDelay);
  }

  void _scheduleFollowingCycle(AnimationStatus status) {
    if (status == AnimationStatus.completed) _startAfter(_betweenCycles);
  }

  void _startAfter(Duration delay) {
    _nextCycle?.cancel();
    _nextCycle = Timer(delay, () {
      if (mounted) _controller.forward(from: 0);
    });
  }

  @override
  void dispose() {
    _nextCycle?.cancel();
    _controller
      ..removeStatusListener(_scheduleFollowingCycle)
      ..dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => RepaintBoundary(
    child: AnimatedBuilder(
      animation: _controller,
      child: widget.child,
      builder: (context, child) {
        final breath = (1 - math.cos(_controller.value * math.pi * 2)) / 2;
        return Transform.translate(
          offset: Offset(0, -2.5 * breath),
          child: Transform.scale(
            key: const Key('noki-idle-breath'),
            alignment: Alignment.bottomCenter,
            scale: 1 + .015 * breath,
            child: child,
          ),
        );
      },
    ),
  );
}

class _NokiThoughtBubble extends StatelessWidget {
  const _NokiThoughtBubble({
    super.key,
    required this.message,
    required this.width,
  });

  final String message;
  final double width;

  // At the text baseline, the hand-drawn body begins inside the rectangular
  // asset by roughly 13% on the left and 14.5% on the right. Keep the desired
  // whitespace inside that visible outline, rather than measuring padding from
  // the transparent edge of the PNG.
  static const _leftOutlineInsetFraction = .13;
  static const _rightOutlineInsetFraction = .145;
  static const _visibleTextSafetyMargin = 26.0;
  static const _singleLineHeight = 120.0;
  static const _additionalLineHeight = 20.0;

  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final textStyle = DefaultTextStyle.of(context).style;
      final textScaler = MediaQuery.textScalerOf(context);
      final textPainter = TextPainter(
        text: TextSpan(text: message, style: textStyle),
        textDirection: Directionality.of(context),
        textScaler: textScaler,
      )..layout();
      const outlineInsetFraction =
          _leftOutlineInsetFraction + _rightOutlineInsetFraction;
      final requiredBubbleWidth =
          (textPainter.width + 2 * _visibleTextSafetyMargin) /
          (1 - outlineInsetFraction);
      final viewportSafeMaxWidth = constraints.maxWidth;
      final bubbleWidth = math.min(
        viewportSafeMaxWidth,
        math.max(width, requiredBubbleWidth),
      );
      final leftTextInset =
          bubbleWidth * _leftOutlineInsetFraction + _visibleTextSafetyMargin;
      final rightTextInset =
          bubbleWidth * _rightOutlineInsetFraction + _visibleTextSafetyMargin;
      final textMaxWidth = math.max(
        0.0,
        bubbleWidth - leftTextInset - rightTextInset,
      );
      textPainter.layout(maxWidth: textMaxWidth);
      final extraLines = math.max(
        0,
        textPainter.computeLineMetrics().length - 1,
      );
      final bubbleHeight =
          _singleLineHeight + extraLines * _additionalLineHeight;

      return IgnorePointer(
        child: SizedBox(
          width: bubbleWidth,
          height: bubbleHeight,
          child: Stack(
            fit: StackFit.expand,
            children: [
              Opacity(
                opacity: .9,
                child: Image.asset(
                  'assets/images/ui/thought_bubble.png',
                  fit: BoxFit.fill,
                  excludeFromSemantics: true,
                ),
              ),
              Positioned.fill(
                key: const Key('noki-thought-text-area'),
                child: Padding(
                  padding: EdgeInsets.fromLTRB(
                    leftTextInset,
                    12,
                    rightTextInset,
                    12,
                  ),
                  child: Transform.translate(
                    key: const Key('noki-thought-text-offset'),
                    offset: const Offset(0, -7),
                    child: Center(
                      child: Text(
                        message,
                        textAlign: TextAlign.center,
                        textScaler: textScaler,
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ),
        ),
      );
    },
  );
}

class _NokiBubblePositionDelegate extends SingleChildLayoutDelegate {
  const _NokiBubblePositionDelegate({
    required this.sceneWidth,
    required this.characterLeft,
    required this.characterSize,
    required this.horizontalOffset,
  });

  final double sceneWidth;
  final double characterLeft;
  final double characterSize;
  final double horizontalOffset;

  @override
  BoxConstraints getConstraintsForChild(BoxConstraints constraints) =>
      BoxConstraints(
        maxWidth: sceneWidth - 16,
        maxHeight: constraints.maxHeight,
      );

  @override
  Offset getPositionForChild(Size size, Size childSize) {
    const screenPadding = 8.0;
    const rightOffset = 12.0;
    const upwardOffset = 40.0;
    final desiredScreenLeft =
        characterLeft + horizontalOffset + characterSize * .36 + rightOffset;
    final maximumScreenLeft = math.max(
      screenPadding,
      sceneWidth - childSize.width - screenPadding,
    );
    final screenLeft = desiredScreenLeft.clamp(
      screenPadding,
      maximumScreenLeft,
    );
    return Offset(
      screenLeft - characterLeft - horizontalOffset,
      size.height - characterSize * .68 - upwardOffset - childSize.height,
    );
  }

  @override
  bool shouldRelayout(covariant _NokiBubblePositionDelegate oldDelegate) =>
      sceneWidth != oldDelegate.sceneWidth ||
      characterLeft != oldDelegate.characterLeft ||
      characterSize != oldDelegate.characterSize ||
      horizontalOffset != oldDelegate.horizontalOffset;
}

class _IdleNokiGroup extends StatefulWidget {
  const _IdleNokiGroup({
    required this.characterSize,
    required this.sceneWidth,
    required this.characterLeft,
    required this.statusMessage,
    required this.noki,
  });

  final double characterSize;
  final double sceneWidth;
  final double characterLeft;
  final String? statusMessage;
  final Widget noki;

  @override
  State<_IdleNokiGroup> createState() => _IdleNokiGroupState();
}

class _IdleNokiGroupState extends State<_IdleNokiGroup>
    with TickerProviderStateMixin {
  static const _wanderRange = 48.0;
  static const _wanderDuration = Duration(seconds: 5);
  static const _wanderInitialDelay = Duration(seconds: 6);
  static const _betweenWanderLegs = Duration(milliseconds: 200);
  static const _initialWelcomeDuration = Duration(seconds: 3);
  static const _idleBubbleHiddenDuration = Duration(seconds: 10);
  static const _idleBubbleVisibleDuration = Duration(seconds: 6);
  static const _idleBubbleFadeDuration = Duration(milliseconds: 350);
  static const _idleMessages = [
    '今天也要整理得整整齊齊。',
    '嗯……家裡還缺什麼呢？',
    '要不要看看庫存？',
    '今天也辛苦啦。',
    'Noki 有好好看家喔。',
    '購物車是不是又多了東西……',
    '冰箱裡還有什麼呢？',
  ];
  static const _wanderTargets = [-_wanderRange, 0.0, _wanderRange, 0.0];

  late final AnimationController _wanderController;
  late final AnimationController _bubbleFadeController;
  Timer? _nextWander;
  Timer? _bubbleTimer;
  Timer? _welcomeTimer;
  var _wanderTargetIndex = 0;
  var _wanderFrom = 0.0;
  var _wanderTo = -_wanderRange;
  var _idleMessageIndex = 0;
  var _idleMessage = _idleMessages.first;
  var _showingWelcome = true;
  var _showingIdleBubble = false;
  var _hidingIdleBubble = false;

  bool get _movingRight => _wanderTo > _wanderFrom;

  @override
  void initState() {
    super.initState();
    _wanderController = AnimationController(
      duration: _wanderDuration,
      vsync: this,
    )..addStatusListener(_onWanderStatus);
    _bubbleFadeController = AnimationController(
      duration: _idleBubbleFadeDuration,
      vsync: this,
    );
    _startWanderAfter(_wanderInitialDelay);
    _welcomeTimer = Timer(_initialWelcomeDuration, _finishInitialWelcome);
  }

  @override
  void didUpdateWidget(covariant _IdleNokiGroup oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.statusMessage == widget.statusMessage) return;
    if (widget.statusMessage != null) {
      _bubbleTimer?.cancel();
      _bubbleFadeController.stop();
      _hidingIdleBubble = false;
      _showingIdleBubble = false;
    } else if (!_showingWelcome) {
      _scheduleIdleBubbleAfter(_idleBubbleHiddenDuration);
    }
  }

  void _finishInitialWelcome() {
    if (!mounted) return;
    setState(() => _showingWelcome = false);
    if (widget.statusMessage == null) {
      _scheduleIdleBubbleAfter(_idleBubbleHiddenDuration);
    }
  }

  void _onWanderStatus(AnimationStatus status) {
    if (status != AnimationStatus.completed) return;
    _wanderFrom = _wanderTo;
    _wanderTargetIndex = (_wanderTargetIndex + 1) % _wanderTargets.length;
    _wanderTo = _wanderTargets[_wanderTargetIndex];
    _wanderController.reset();
    _startWanderAfter(_betweenWanderLegs);
  }

  void _startWanderAfter(Duration delay) {
    _nextWander?.cancel();
    _nextWander = Timer(delay, () {
      if (mounted) _wanderController.forward(from: 0);
    });
  }

  void _scheduleIdleBubbleAfter(Duration delay) {
    _bubbleTimer?.cancel();
    _bubbleTimer = Timer(delay, _showIdleBubble);
  }

  void _showIdleBubble() {
    if (!mounted || widget.statusMessage != null) return;
    setState(() {
      _idleMessage = _idleMessages[_idleMessageIndex];
      _idleMessageIndex = (_idleMessageIndex + 1) % _idleMessages.length;
      _showingIdleBubble = true;
      _hidingIdleBubble = false;
    });
    _bubbleFadeController.forward(from: 0);
    _bubbleTimer = Timer(_idleBubbleVisibleDuration, _hideIdleBubble);
  }

  void _hideIdleBubble() {
    _hidingIdleBubble = true;
    _bubbleFadeController.reverse();
    _bubbleTimer = Timer(_idleBubbleFadeDuration, () {
      if (!mounted || widget.statusMessage != null || !_hidingIdleBubble) {
        return;
      }
      setState(() {
        _hidingIdleBubble = false;
        _showingIdleBubble = false;
      });
      _scheduleIdleBubbleAfter(_idleBubbleHiddenDuration);
    });
  }

  @override
  void dispose() {
    _nextWander?.cancel();
    _bubbleTimer?.cancel();
    _welcomeTimer?.cancel();
    _wanderController
      ..removeStatusListener(_onWanderStatus)
      ..dispose();
    _bubbleFadeController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final bubbleWidth = widget.characterSize * .68;
    return AnimatedBuilder(
      animation: Listenable.merge([_wanderController, _bubbleFadeController]),
      builder: (context, child) {
        final progress = (1 - math.cos(_wanderController.value * math.pi)) / 2;
        final horizontalOffset =
            _wanderFrom + (_wanderTo - _wanderFrom) * progress;
        final bubble =
            widget.statusMessage ?? (_showingWelcome ? '歡迎回家。' : _idleMessage);
        final showBubble =
            widget.statusMessage != null ||
            _showingWelcome ||
            _showingIdleBubble;
        return Transform.translate(
          key: const Key('noki-idle-wander'),
          offset: Offset(horizontalOffset, 0),
          child: Stack(
            clipBehavior: Clip.none,
            children: [
              Positioned.fill(
                child: Transform(
                  key: const Key('noki-idle-sprite'),
                  alignment: Alignment.center,
                  transform: Matrix4.diagonal3Values(
                    _movingRight ? -1 : 1,
                    1,
                    1,
                  ),
                  child: SizedBox(
                    key: const Key('noki-idle-sprite-content'),
                    child: widget.noki,
                  ),
                ),
              ),
              if (showBubble)
                Positioned.fill(
                  child: CustomSingleChildLayout(
                    delegate: _NokiBubblePositionDelegate(
                      sceneWidth: widget.sceneWidth,
                      characterLeft: widget.characterLeft,
                      characterSize: widget.characterSize,
                      horizontalOffset: horizontalOffset,
                    ),
                    child: widget.statusMessage != null || _showingWelcome
                        ? _NokiThoughtBubble(
                            key: const Key('speech'),
                            message: bubble,
                            width: bubbleWidth,
                          )
                        : FadeTransition(
                            key: const Key('idle-noki-bubble'),
                            opacity: _bubbleFadeController,
                            child: _NokiThoughtBubble(
                              key: const Key('speech'),
                              message: bubble,
                              width: bubbleWidth,
                            ),
                          ),
                  ),
                ),
            ],
          ),
        );
      },
    );
  }
}

class HomeScene extends StatelessWidget {
  const HomeScene({
    super.key,
    required this.onInventory,
    required this.onShopping,
    required this.onBackgroundTap,
    this.statusMessage,
    this.workSurfaceOpen = false,
  });
  final VoidCallback onInventory;
  final VoidCallback onShopping;
  final VoidCallback onBackgroundTap;
  final String? statusMessage;
  final bool workSurfaceOpen;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final width = constraints.maxWidth;
      final height = constraints.maxHeight;
      final cabinetWidth = width * .406;
      final cabinetHeight = height * .672;
      final imageWidth = math.min(cabinetWidth, cabinetHeight / 2);
      final imageHeight = imageWidth * 2;
      final cartHeight = imageHeight * .51;
      final cartWidth = cartHeight * (1071 / 1118);
      final originalCharacterSize =
          math.min(width * .34, height * .31).clamp(44.0, 150.0) * 1.4;
      final characterSize = originalCharacterSize * .9 * 1.15;
      final characterLeft = (width - characterSize) / 2;
      final characterBottom = height * .12 - characterSize / 2;
      return Container(
        key: const Key('room'),
        decoration: BoxDecoration(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Stack(
          key: const Key('room-scene-stack'),
          clipBehavior: Clip.none,
          children: [
            Positioned.fill(
              child: GestureDetector(
                key: const Key('room-background-tap'),
                behavior: HitTestBehavior.opaque,
                onTap: onBackgroundTap,
              ),
            ),
            if (workSurfaceOpen)
              Positioned(
                left: width * .08,
                top: 20,
                height: height * .28,
                width: width * .84,
                child: LayoutBuilder(
                  builder: (context, available) {
                    final size = math.min(
                      available.maxHeight * .85,
                      width * .36,
                    );
                    return Stack(
                      children: [
                        Positioned(
                          left: 0,
                          bottom: 0,
                          child: Semantics(
                            label: '窩有小幫手',
                            child: IgnorePointer(
                              child: Image.asset(
                                'assets/images/agent/noki_original.png',
                                key: const Key('agent'),
                                width: size,
                                height: size,
                                fit: BoxFit.contain,
                                excludeFromSemantics: true,
                              ),
                            ),
                          ),
                        ),
                        if (statusMessage != null)
                          Positioned(
                            left: size * .8,
                            top: 0,
                            child: _NokiThoughtBubble(
                              key: const Key('speech'),
                              message: statusMessage!,
                              width: math.min(
                                width * .38,
                                math.max(0, available.maxWidth - size * .8),
                              ),
                            ),
                          ),
                      ],
                    );
                  },
                ),
              ),
            if (!workSurfaceOpen) ...[
              Positioned(
                right: width * .085,
                top: height * .04,
                child: IgnorePointer(
                  child: SizedBox(
                    key: const Key('window'),
                    width: math.min(width * .23, height * .20),
                    child: AspectRatio(
                      aspectRatio: 890 / 1060,
                      child: SceneWindow(
                        weather: Image.asset(
                          'assets/images/weather/weather_test.png',
                          fit: BoxFit.cover,
                          excludeFromSemantics: true,
                        ),
                      ),
                    ),
                  ),
                ),
              ),
              Positioned(
                left: width * .01,
                bottom: height * .12,
                child: IgnorePointer(
                  child: Image.asset(
                    'assets/images/objects/carpet.png',
                    width: math.min(width * .98, height * .85),
                    fit: BoxFit.contain,
                    excludeFromSemantics: true,
                  ),
                ),
              ),
              Align(
                key: const Key('cabinet-layer'),
                alignment: RoomAnchors.cabinet,
                child: Transform.translate(
                  offset: const Offset(0, 20),
                  child: SizedBox(
                    width: cabinetWidth,
                    height: cabinetHeight,
                    child: Stack(
                      children: [
                        Positioned.fill(
                          child: Image.asset(
                            'assets/images/objects/storage_cabinet.png',
                            fit: BoxFit.contain,
                            excludeFromSemantics: true,
                          ),
                        ),
                        Positioned(
                          left:
                              (cabinetWidth - imageWidth) / 2 +
                              imageWidth * .10,
                          top:
                              (cabinetHeight - imageHeight) / 2 +
                              imageHeight * .09,
                          width: imageWidth * .80,
                          height: imageHeight * .75,
                          child: Semantics(
                            label: '庫存櫃',
                            button: true,
                            child: Material(
                              color: Colors.transparent,
                              child: InkWell(
                                key: const Key('cabinet'),
                                onTap: onInventory,
                              ),
                            ),
                          ),
                        ),
                      ],
                    ),
                  ),
                ),
              ),
              Positioned(
                key: const Key('beanbag-layer'),
                left: width * .01,
                bottom: height * .065 + 70,
                child: IgnorePointer(
                  child: Image.asset(
                    'assets/images/objects/beanbag.png',
                    key: const Key('beanbag'),
                    width: math.min(width * .5, height * .4) * .9,
                    fit: BoxFit.contain,
                    excludeFromSemantics: true,
                  ),
                ),
              ),
              Positioned(
                right: width * .0125,
                top: height * .43 + 20,
                child: Semantics(
                  label: '購物車',
                  button: true,
                  child: InkWell(
                    key: const Key('cart'),
                    onTap: onShopping,
                    child: SizedBox(
                      width: cartWidth,
                      height: cartHeight,
                      child: Image.asset(
                        'assets/images/objects/cart.png',
                        fit: BoxFit.contain,
                        excludeFromSemantics: true,
                      ),
                    ),
                  ),
                ),
              ),
              Positioned(
                left: characterLeft,
                bottom: characterBottom + 10,
                child: SizedBox(
                  key: const Key('agent'),
                  width: characterSize,
                  height: characterSize,
                  child: _IdleNokiGroup(
                    characterSize: characterSize,
                    sceneWidth: width,
                    characterLeft: characterLeft,
                    statusMessage: statusMessage,
                    noki: _IdleNoki(
                      child: Semantics(
                        label: '窩有小幫手',
                        child: IgnorePointer(
                          child: Image.asset(
                            'assets/images/agent/noki_original.png',
                            fit: BoxFit.contain,
                            excludeFromSemantics: true,
                          ),
                        ),
                      ),
                    ),
                  ),
                ),
              ),
            ],
          ],
        ),
      );
    },
  );
}
