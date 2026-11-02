import 'package:flutter/material.dart';

class LoadingState extends StatelessWidget {
  const LoadingState({super.key});
  @override
  Widget build(BuildContext context) => const Padding(
    padding: EdgeInsets.all(16),
    child: Center(child: CircularProgressIndicator(semanticsLabel: '載入中')),
  );
}

class ErrorState extends StatelessWidget {
  const ErrorState({super.key, required this.onRetry, this.timeout = false});
  final VoidCallback onRetry;
  final bool timeout;
  @override
  Widget build(BuildContext context) => Column(
    mainAxisSize: MainAxisSize.min,
    children: [
      Text(timeout ? '等待逾時，請重試。' : '暫時無法完成，請重試。'),
      TextButton(onPressed: onRetry, child: const Text('重試')),
    ],
  );
}
