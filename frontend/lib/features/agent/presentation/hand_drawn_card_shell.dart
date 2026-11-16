import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Shared Nestock card frame for read-only results and actionable approvals.
class HandDrawnCardShell extends StatelessWidget {
  const HandDrawnCardShell({
    super.key,
    required this.child,
    this.contentPadding = const EdgeInsets.fromLTRB(18, 18, 18, 14),
  });

  final Widget child;
  final EdgeInsetsGeometry contentPadding;

  @override
  Widget build(BuildContext context) => Stack(
    clipBehavior: Clip.none,
    children: [
      Padding(
        padding: const EdgeInsets.all(26),
        child: Material(
          color: Theme.of(context).colorScheme.surfaceContainerLowest,
          borderRadius: BorderRadius.circular(14),
          child: Padding(padding: contentPadding, child: child),
        ),
      ),
      const HandDrawnCardBorder(),
    ],
  );
}

class HandDrawnCardBorder extends StatelessWidget {
  const HandDrawnCardBorder({super.key});

  static const _strokeThickness = 22.0;
  static const _cornerInset = _strokeThickness;

  @override
  Widget build(BuildContext context) => Positioned.fill(
    child: IgnorePointer(
      child: Stack(
        clipBehavior: Clip.none,
        children: [
          Positioned(
            key: const Key('confirmation-border-top'),
            left: _cornerInset,
            right: _cornerInset,
            top: 0,
            height: _strokeThickness,
            child: Image.asset(
              'assets/images/ui/confirmation_border_horizontal.png',
              fit: BoxFit.fill,
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-bottom'),
            left: _cornerInset,
            right: _cornerInset,
            bottom: 0,
            height: _strokeThickness,
            child: Transform.rotate(
              key: const Key('confirmation-border-bottom-rotate'),
              angle: math.pi,
              child: Image.asset(
                'assets/images/ui/confirmation_border_horizontal.png',
                fit: BoxFit.fill,
              ),
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-left'),
            left: 0,
            top: _cornerInset,
            bottom: _cornerInset,
            width: _strokeThickness,
            child: Image.asset(
              'assets/images/ui/confirmation_border_vertical.png',
              fit: BoxFit.fill,
            ),
          ),
          Positioned(
            key: const Key('confirmation-border-right'),
            right: 0,
            top: _cornerInset,
            bottom: _cornerInset,
            width: _strokeThickness,
            child: Transform.rotate(
              key: const Key('confirmation-border-right-rotate'),
              angle: math.pi,
              child: Image.asset(
                'assets/images/ui/confirmation_border_vertical.png',
                fit: BoxFit.fill,
              ),
            ),
          ),
        ],
      ),
    ),
  );
}
