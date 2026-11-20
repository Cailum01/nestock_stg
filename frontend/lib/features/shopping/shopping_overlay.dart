import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import 'commerce_options_client.dart';
import 'shopping_view_client.dart';

class ShoppingOverlay extends StatefulWidget {
  const ShoppingOverlay({
    super.key,
    required this.client,
    required this.onClose,
    this.commerceClient = const UnavailableCommerceOptionsClient(),
    this.onOpenProductUrl,
    this.createRequestId = newShoppingRequestId,
  });
  final ShoppingViewClient client;
  final VoidCallback onClose;
  final CommerceOptionsClient commerceClient;
  final Future<bool> Function(Uri url)? onOpenProductUrl;
  final String Function() createRequestId;
  @override
  State<ShoppingOverlay> createState() => _ShoppingOverlayState();
}

class _ShoppingOverlayState extends State<ShoppingOverlay> {
  late Future<List<ShoppingViewItem>> _items;
  final _nameController = TextEditingController();
  final _quantityController = TextEditingController();
  final _unitController = TextEditingController();
  bool _adding = false;
  bool _saving = false;
  String? _addError;
  ({String requestId, String name, int? quantity, String? unit})? _pendingAdd;
  final _commerceRequests = <String>{};

  @override
  void initState() {
    super.initState();
    _items = widget.client.load();
  }

  @override
  void didUpdateWidget(covariant ShoppingOverlay oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.client != widget.client) _items = widget.client.load();
  }

  void _openAdd() => setState(() {
    _adding = true;
    _addError = null;
  });

  void _cancelAdd() => setState(() {
    _adding = false;
    _pendingAdd = null;
    _addError = null;
    _nameController.clear();
    _quantityController.clear();
    _unitController.clear();
  });

  Future<void> _addItem() async {
    if (_saving) return;
    final addClient = widget.client;
    if (addClient is! ShoppingManualAddClient) {
      setState(() => _addError = '目前無法儲存購物項目，請稍後再試。');
      return;
    }

    final pending = _pendingAdd;
    if (pending == null) {
      final name = _nameController.text;
      if (name.trim().isEmpty) {
        setState(() => _addError = '請輸入商品名稱。');
        return;
      }
      final rawQuantity = _quantityController.text.trim();
      final quantity = rawQuantity.isEmpty ? null : int.tryParse(rawQuantity);
      if (rawQuantity.isNotEmpty && (quantity == null || quantity < 1)) {
        setState(() => _addError = '數量必須是大於 0 的整數。');
        return;
      }
      final rawUnit = _unitController.text;
      final unit = rawUnit.trim().isEmpty ? null : rawUnit;
      _pendingAdd = (
        requestId: widget.createRequestId(),
        name: name,
        quantity: quantity,
        unit: unit,
      );
    }

    final request = _pendingAdd!;
    setState(() {
      _saving = true;
      _addError = null;
    });
    try {
      await addClient.add(
        requestId: request.requestId,
        name: request.name,
        quantity: request.quantity,
        unit: request.unit,
      );
      final refreshedItems = await widget.client.load();
      if (!mounted) return;
      setState(() {
        _items = Future.value(refreshedItems);
        _adding = false;
        _saving = false;
        _addError = null;
        _pendingAdd = null;
        _nameController.clear();
        _quantityController.clear();
        _unitController.clear();
      });
    } catch (error) {
      if (!mounted) return;
      setState(() {
        _saving = false;
        _addError = _messageForAddFailure(error);
      });
    }
  }

  String _messageForAddFailure(Object error) {
    if (error is ShoppingAddException) {
      return switch (error.failure) {
        ShoppingAddFailure.validation => '商品資料無效，請確認後重試。',
        ShoppingAddFailure.unauthenticated => '登入狀態已失效，請重新登入後再試。',
        ShoppingAddFailure.forbidden => '目前無權限存取此家庭，請切換家庭後重試。',
        ShoppingAddFailure.requestConflict => '這次新增資料發生衝突，請稍後確認購物清單。',
        ShoppingAddFailure.unavailable => '目前無法儲存購物項目，請稍後再試。',
        ShoppingAddFailure.timeout => '連線逾時，請重試。',
        ShoppingAddFailure.network => '連線失敗，請重試。',
        ShoppingAddFailure.malformedResponse ||
        ShoppingAddFailure.unexpectedResponse => '伺服器回應異常，請稍後再試。',
      };
    }
    return '目前無法儲存購物項目，請稍後再試。';
  }

  Future<void> _showRecommendations(ShoppingViewItem item) async {
    if (!_commerceRequests.add(item.name)) return;
    setState(() {});
    CommerceOptionsView result;
    try {
      result = await widget.commerceClient.lookup(item.name);
    } catch (_) {
      result = const CommerceOptionsView(
        status: CommerceOptionsStatus.unavailable,
        message: '目前無法取得採購推薦，請稍後再試。',
      );
    } finally {
      if (mounted) setState(() => _commerceRequests.remove(item.name));
    }
    if (!mounted) return;
    await showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (context) => _CommerceRecommendationSheet(
        keyword: item.name,
        result: result,
        onOpenProductUrl: _openProductUrl,
      ),
    );
  }

  Future<void> _openProductUrl(Uri url) async {
    try {
      final opened =
          await (widget.onOpenProductUrl?.call(url) ??
              launchUrl(url, mode: LaunchMode.externalApplication));
      if (!opened) throw StateError('launch-failed');
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('無法開啟商品連結，請稍後再試。')));
      }
    }
  }

  @override
  void dispose() {
    _nameController.dispose();
    _quantityController.dispose();
    _unitController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Material(
    key: const Key('shopping-overlay'),
    color: Theme.of(context).colorScheme.surface,
    borderRadius: BorderRadius.circular(16),
    elevation: 4,
    child: Padding(
      padding: const EdgeInsets.all(12),
      child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Row(
            children: [
              const Expanded(child: Text('購物清單')),
              IconButton(
                onPressed: _saving || _pendingAdd != null
                    ? null
                    : widget.onClose,
                icon: const Icon(Icons.close),
                tooltip: '關閉購物清單',
              ),
            ],
          ),
          Flexible(
            child: FutureBuilder<List<ShoppingViewItem>>(
              future: _items,
              builder: (context, snapshot) {
                if (snapshot.hasError) return const Text('無法載入購物清單。');
                if (!snapshot.hasData) return const CircularProgressIndicator();
                final data = snapshot.data!;
                return Column(
                  mainAxisSize: MainAxisSize.min,
                  children: [
                    Text(
                      '待購 ${data.where((item) => item.status == 'pending').length} 件',
                    ),
                    if (data.isEmpty && !_adding)
                      const Padding(
                        padding: EdgeInsets.all(16),
                        child: Text('購物清單目前是空的。'),
                      )
                    else
                      Flexible(
                        child: ListView.builder(
                          key: const Key('shopping-items'),
                          shrinkWrap: true,
                          itemCount: data.length,
                          itemBuilder: (context, index) {
                            final item = data[index];
                            return ListTile(
                              dense: true,
                              title: Text(item.name),
                              trailing: _commerceRequests.contains(item.name)
                                  ? const SizedBox(
                                      width: 24,
                                      height: 24,
                                      child: CircularProgressIndicator(
                                        strokeWidth: 2,
                                      ),
                                    )
                                  : TextButton(
                                      key: Key('shopping-procure-${item.name}'),
                                      onPressed: () =>
                                          _showRecommendations(item),
                                      child: const Text('採購 >'),
                                    ),
                            );
                          },
                        ),
                      ),
                    if (_adding) ...[
                      const SizedBox(height: 8),
                      TextField(
                        key: const Key('shopping-add-name'),
                        controller: _nameController,
                        readOnly: _pendingAdd != null,
                        textInputAction: TextInputAction.done,
                        decoration: const InputDecoration(labelText: '商品名稱'),
                      ),
                      TextField(
                        key: const Key('shopping-add-quantity'),
                        controller: _quantityController,
                        readOnly: _pendingAdd != null,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(labelText: '數量（選填）'),
                      ),
                      TextField(
                        key: const Key('shopping-add-unit'),
                        controller: _unitController,
                        readOnly: _pendingAdd != null,
                        decoration: const InputDecoration(labelText: '單位（選填）'),
                      ),
                      if (_addError != null)
                        Padding(
                          padding: const EdgeInsets.only(top: 4),
                          child: Text(
                            _addError!,
                            style: TextStyle(
                              color: Theme.of(context).colorScheme.error,
                            ),
                          ),
                        ),
                      Align(
                        alignment: Alignment.centerRight,
                        child: Wrap(
                          spacing: 8,
                          children: [
                            TextButton(
                              onPressed: _saving || _pendingAdd != null
                                  ? null
                                  : _cancelAdd,
                              child: const Text('取消'),
                            ),
                            FilledButton(
                              onPressed: _saving ? null : _addItem,
                              child: const Text('加入'),
                            ),
                          ],
                        ),
                      ),
                    ] else
                      Align(
                        alignment: Alignment.centerLeft,
                        child: TextButton.icon(
                          key: const Key('shopping-add-entry'),
                          onPressed: _openAdd,
                          icon: const Icon(Icons.add),
                          label: const Text('新增購物項目'),
                        ),
                      ),
                  ],
                );
              },
            ),
          ),
        ],
      ),
    ),
  );
}

class _CommerceRecommendationSheet extends StatelessWidget {
  const _CommerceRecommendationSheet({
    required this.keyword,
    required this.result,
    required this.onOpenProductUrl,
  });

  final String keyword;
  final CommerceOptionsView result;
  final Future<void> Function(Uri url) onOpenProductUrl;

  @override
  Widget build(BuildContext context) {
    final options = result.options;
    final hasOptions =
        result.status == CommerceOptionsStatus.success && options.isNotEmpty;
    final message = hasOptions
        ? null
        : result.status == CommerceOptionsStatus.noMatch ||
              result.status == CommerceOptionsStatus.success
        ? '找不到「$keyword」的採購推薦。'
        : '目前無法取得採購推薦，請稍後再試。';
    return SafeArea(
      child: LayoutBuilder(
        builder: (context, constraints) => ConstrainedBox(
          constraints: BoxConstraints(maxHeight: constraints.maxHeight * .75),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(20, 0, 20, 12),
                child: Text(
                  '推薦採購',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
              ),
              if (message != null)
                Padding(
                  padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
                  child: Text(message),
                )
              else
                Flexible(
                  child: ListView.separated(
                    key: const Key('commerce-options-list'),
                    shrinkWrap: true,
                    padding: const EdgeInsets.fromLTRB(20, 0, 20, 24),
                    itemCount: options.length,
                    separatorBuilder: (_, _) => const SizedBox(height: 10),
                    itemBuilder: (context, index) => _CommerceOptionTile(
                      option: options[index],
                      onOpenProductUrl: onOpenProductUrl,
                    ),
                  ),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

class _CommerceOptionTile extends StatelessWidget {
  const _CommerceOptionTile({
    required this.option,
    required this.onOpenProductUrl,
  });

  final CommerceOptionView option;
  final Future<void> Function(Uri url) onOpenProductUrl;

  Uri? get _productUri {
    final rawUrl = option.productUrl;
    final uri = rawUrl == null ? null : Uri.tryParse(rawUrl);
    return uri != null &&
            (uri.scheme == 'https' || uri.scheme == 'http') &&
            uri.hasAuthority
        ? uri
        : null;
  }

  @override
  Widget build(BuildContext context) {
    final uri = _productUri;
    return Card(
      margin: const EdgeInsets.only(bottom: 10),
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(option.title, style: Theme.of(context).textTheme.titleSmall),
            if (option.store != null) ...[
              const SizedBox(height: 4),
              Text(option.store!),
            ],
            if (option.estimatedPrice != null) ...[
              const SizedBox(height: 4),
              Text('NT\$${_priceText(option.estimatedPrice!)}'),
            ],
            if (option.unitSpec != null) ...[
              const SizedBox(height: 4),
              Text(option.unitSpec!),
            ],
            if (uri != null)
              Align(
                alignment: Alignment.centerRight,
                child: TextButton(
                  key: Key('commerce-open-${option.title}'),
                  onPressed: () => onOpenProductUrl(uri),
                  child: const Text('前往購買 >'),
                ),
              ),
          ],
        ),
      ),
    );
  }
}

String _priceText(num value) => value == value.truncateToDouble()
    ? value.toStringAsFixed(0)
    : value.toString();
