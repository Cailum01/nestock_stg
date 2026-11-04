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

class HomeScene extends StatelessWidget {
  const HomeScene({
    super.key,
    required this.onInventory,
    required this.onShopping,
    required this.onBackgroundTap,
    required this.message,
    this.workSurfaceOpen = false,
  });
  final VoidCallback onInventory;
  final VoidCallback onShopping;
  final VoidCallback onBackgroundTap;
  final String message;
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
      final characterSize = originalCharacterSize * .9;
      final characterLeft = width * .08;
      final characterBottom =
          height * .075 + math.min(width * .5, height * .4) * .12;
      final bubbleWidth = width * .38;
      return Container(
        key: const Key('room'),
        decoration: BoxDecoration(
          color: Colors.transparent,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Stack(
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
                top: 0,
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
                                'assets/images/agent/noki_sit.png',
                                key: const Key('agent'),
                                width: size,
                                height: size,
                                fit: BoxFit.contain,
                                excludeFromSemantics: true,
                              ),
                            ),
                          ),
                        ),
                        Positioned(
                          left: size * .8,
                          top: 0,
                          child: IgnorePointer(
                            child: SizedBox(
                              key: const Key('speech'),
                              width: math.min(
                                width * .38,
                                math.max(0, available.maxWidth - size * .8),
                              ),
                              child: AspectRatio(
                                aspectRatio: 1536 / 1024,
                                child: Stack(
                                  children: [
                                    Positioned.fill(
                                      child: Image.asset(
                                        'assets/images/objects/thought_bubble.png',
                                        fit: BoxFit.contain,
                                        excludeFromSemantics: true,
                                      ),
                                    ),
                                    Positioned.fill(
                                      bottom: width * .38 * .12,
                                      child: Center(
                                        child: Text(
                                          message,
                                          textAlign: TextAlign.center,
                                          maxLines: 2,
                                          overflow: TextOverflow.ellipsis,
                                          style: const TextStyle(
                                            letterSpacing: 2,
                                          ),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                              ),
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
              Positioned(
                left: width * .01,
                bottom: height * .065,
                child: IgnorePointer(
                  child: Image.asset(
                    'assets/images/objects/beanbag.png',
                    width: math.min(width * .5, height * .4),
                    fit: BoxFit.contain,
                    excludeFromSemantics: true,
                  ),
                ),
              ),
              Align(
                alignment: RoomAnchors.cabinet,
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
                            (cabinetWidth - imageWidth) / 2 + imageWidth * .10,
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
              Positioned(
                right: width * .0125,
                top: height * .43,
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
                bottom: characterBottom,
                child: SizedBox(
                  key: const Key('agent'),
                  width: characterSize,
                  height: characterSize,
                  child: Semantics(
                    label: '窩有小幫手',
                    child: IgnorePointer(
                      child: Image.asset(
                        'assets/images/agent/noki_sit.png',
                        fit: BoxFit.contain,
                        excludeFromSemantics: true,
                      ),
                    ),
                  ),
                ),
              ),
              Positioned(
                left: math.min(
                  width * .10 + originalCharacterSize * .42,
                  width - bubbleWidth,
                ),
                bottom: characterBottom + originalCharacterSize * .83,
                child: SizedBox(
                  key: const Key('speech'),
                  width: bubbleWidth,
                  child: IgnorePointer(
                    child: AspectRatio(
                      aspectRatio: 1536 / 1024,
                      child: Stack(
                        children: [
                          Positioned.fill(
                            child: Image.asset(
                              'assets/images/objects/thought_bubble.png',
                              fit: BoxFit.contain,
                              excludeFromSemantics: true,
                            ),
                          ),
                          Positioned.fill(
                            bottom: width * .38 * .12,
                            child: Center(
                              child: Text(
                                message,
                                textAlign: TextAlign.center,
                                maxLines: 2,
                                overflow: TextOverflow.ellipsis,
                                style: const TextStyle(letterSpacing: 2),
                              ),
                            ),
                          ),
                        ],
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
