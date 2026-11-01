import 'package:flutter/material.dart';

/// Replaceable scene artwork; each visual object owns its own hit target.
class HomeScene extends StatelessWidget {
  const HomeScene({
    super.key,
    required this.onInventory,
    required this.onShopping,
  });
  final VoidCallback onInventory;
  final VoidCallback onShopping;

  @override
  Widget build(BuildContext context) {
    final colors = Theme.of(context).colorScheme;
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: colors.primaryContainer.withValues(alpha: .45),
        borderRadius: BorderRadius.circular(24),
      ),
      child: Column(
        children: [
          Text('窩有的小天地', style: Theme.of(context).textTheme.titleMedium),
          const SizedBox(height: 12),
          CircleAvatar(
            radius: 38,
            backgroundColor: colors.surface,
            child: Icon(
              Icons.smart_toy_outlined,
              size: 48,
              color: colors.primary,
              semanticLabel: '窩有小幫手',
            ),
          ),
          const SizedBox(height: 16),
          Wrap(
            alignment: WrapAlignment.center,
            spacing: 12,
            runSpacing: 8,
            children: [
              _SceneObject(
                icon: Icons.kitchen_outlined,
                label: '打開庫存櫃',
                onTap: onInventory,
              ),
              _SceneObject(
                icon: Icons.shopping_cart_outlined,
                label: '打開購物清單',
                onTap: onShopping,
              ),
            ],
          ),
        ],
      ),
    );
  }
}

class _SceneObject extends StatelessWidget {
  const _SceneObject({
    required this.icon,
    required this.label,
    required this.onTap,
  });
  final IconData icon;
  final String label;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) => OutlinedButton.icon(
    onPressed: onTap,
    icon: Icon(icon, size: 28),
    label: Text(label),
    style: OutlinedButton.styleFrom(
      backgroundColor: Theme.of(context).colorScheme.surface,
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
    ),
  );
}
