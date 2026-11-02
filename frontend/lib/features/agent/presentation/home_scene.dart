import 'dart:math' as math;

import 'package:flutter/material.dart';

/// Scene geometry and overlay origin live together so artwork can replace props.
abstract final class RoomAnchors {
  static const cabinet = Alignment(-.86, -.88);
  static const cart = Alignment(.87, .15);
}

class HomeScene extends StatelessWidget {
  const HomeScene({
    super.key,
    required this.onInventory,
    required this.onShopping,
    required this.message,
  });
  final VoidCallback onInventory;
  final VoidCallback onShopping;
  final String message;
  @override
  Widget build(BuildContext context) => LayoutBuilder(
    builder: (context, constraints) {
      final colors = Theme.of(context).colorScheme;
      final width = constraints.maxWidth;
      final height = constraints.maxHeight;
      final cabinetWidth = width * .29;
      final cabinetHeight = height * .48;
      final characterSize = math
          .min(width * .34, height * .31)
          .clamp(44.0, 150.0);
      return Container(
        key: const Key('room'),
        decoration: BoxDecoration(
          color: colors.secondaryContainer,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Stack(
          children: [
            Align(
              alignment: RoomAnchors.cabinet,
              child: Semantics(
                label: '庫存櫃',
                button: true,
                child: Material(
                  color: Colors.transparent,
                  child: InkWell(
                    key: const Key('cabinet'),
                    onTap: onInventory,
                    child: Container(
                      width: cabinetWidth,
                      height: cabinetHeight,
                      decoration: BoxDecoration(
                        color: colors.surfaceContainerHighest,
                        border: Border.all(color: colors.outline, width: 2),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: Row(
                        children: [
                          Expanded(
                            child: Center(
                              child: Container(
                                width: 3,
                                height: 14,
                                color: colors.outline,
                              ),
                            ),
                          ),
                          Container(width: 1, color: colors.outline),
                          Expanded(
                            child: Center(
                              child: Container(
                                width: 3,
                                height: 14,
                                color: colors.outline,
                              ),
                            ),
                          ),
                        ],
                      ),
                    ),
                  ),
                ),
              ),
            ),
            Align(
              alignment: const Alignment(.82, -.83),
              child: Container(
                key: const Key('window'),
                width: width * .23,
                height: height * .20,
                decoration: BoxDecoration(
                  color: colors.surface,
                  border: Border.all(color: colors.outline, width: 3),
                ),
                child: Center(
                  child: Container(width: 2, color: colors.outline),
                ),
              ),
            ),
            Align(
              alignment: RoomAnchors.cart,
              child: Semantics(
                label: '購物車',
                button: true,
                child: InkWell(
                  key: const Key('cart'),
                  onTap: onShopping,
                  child: SizedBox(
                    width: width * .20,
                    height: math.max(48, height * .20),
                    child: Icon(
                      Icons.shopping_cart_outlined,
                      size: math.min(width * .16, 64),
                      color: colors.primary,
                    ),
                  ),
                ),
              ),
            ),
            Align(
              alignment: const Alignment(0, .55),
              child: SizedBox(
                key: const Key('agent'),
                width: characterSize,
                height: characterSize,
                child: Semantics(
                  label: '窩有小幫手',
                  child: DecoratedBox(
                    decoration: BoxDecoration(
                      color: colors.surface,
                      border: Border.all(color: colors.outline, width: 2),
                      borderRadius: BorderRadius.circular(characterSize * .43),
                    ),
                    child: Center(
                      child: Icon(
                        Icons.face_outlined,
                        size: characterSize * .45,
                        color: colors.primary,
                      ),
                    ),
                  ),
                ),
              ),
            ),
            Align(
              alignment: const Alignment(0, -.10),
              child: Container(
                key: const Key('speech'),
                constraints: BoxConstraints(maxWidth: width * .48),
                padding: const EdgeInsets.all(8),
                decoration: BoxDecoration(
                  color: colors.surface,
                  borderRadius: BorderRadius.circular(16),
                ),
                child: Text(
                  message,
                  textAlign: TextAlign.center,
                  maxLines: 2,
                  overflow: TextOverflow.ellipsis,
                ),
              ),
            ),
          ],
        ),
      );
    },
  );
}
