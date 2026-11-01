import 'package:flutter/material.dart';

/// Replaceable room placeholders; positions scale with the available room.
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
      final compact = constraints.maxHeight < 300;
      return Container(
        key: const Key('room'),
        decoration: BoxDecoration(
          color: colors.secondaryContainer,
          borderRadius: BorderRadius.circular(20),
        ),
        child: Stack(
          children: [
            Positioned(
              top: 8,
              left: 8,
              child: _SceneObject(
                key: const Key('cabinet'),
                icon: Icons.kitchen_outlined,
                label: '庫存櫃',
                onTap: onInventory,
              ),
            ),
            Positioned(
              top: 8,
              right: 8,
              child: _SceneObject(
                key: const Key('cart'),
                icon: Icons.shopping_cart_outlined,
                label: '購物車',
                onTap: onShopping,
              ),
            ),
            Center(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 54),
                child: Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Container(
                      key: const Key('speech'),
                      padding: const EdgeInsets.all(8),
                      decoration: BoxDecoration(
                        color: colors.surface,
                        borderRadius: BorderRadius.circular(16),
                      ),
                      child: Text(
                        message,
                        maxLines: compact ? 2 : 4,
                        overflow: TextOverflow.ellipsis,
                        textAlign: TextAlign.center,
                      ),
                    ),
                    const SizedBox(height: 8),
                    CircleAvatar(
                      key: const Key('agent'),
                      radius: compact ? 26 : 38,
                      backgroundColor: colors.surface,
                      child: Icon(
                        Icons.smart_toy_outlined,
                        size: compact ? 36 : 48,
                        color: colors.primary,
                        semanticLabel: '窩有小幫手',
                      ),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      );
    },
  );
}

class _SceneObject extends StatelessWidget {
  const _SceneObject({
    super.key,
    required this.icon,
    required this.label,
    required this.onTap,
  });
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => Semantics(
    button: true,
    label: label,
    child: InkWell(
      onTap: onTap,
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Icon(icon, size: 36),
      ),
    ),
  );
}
