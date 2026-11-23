import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../data/identity_api.dart';
import '../domain/identity_controller.dart';
import '../domain/identity_models.dart';
import '../../../features/agent/presentation/hand_drawn_card_shell.dart';
import '../../../shared/theme/app_theme.dart';

typedef AuthenticatedHomeBuilder = Widget Function(
  BuildContext context,
  IdentityController controller,
);

class IdentityGate extends StatefulWidget {
  const IdentityGate({
    super.key,
    required this.controller,
    required this.authenticatedHomeBuilder,
    this.connectHouseholdOnStart = false,
  });

  final IdentityController controller;
  final AuthenticatedHomeBuilder authenticatedHomeBuilder;
  final bool connectHouseholdOnStart;

  @override
  State<IdentityGate> createState() => _IdentityGateState();
}

class _IdentityGateState extends State<IdentityGate> {
  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_changed);
    if (!widget.controller.initialized) {
      widget.controller.initialize(
        launchGoogleSignIn: !widget.connectHouseholdOnStart,
      );
    }
  }

  @override
  void didUpdateWidget(covariant IdentityGate oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_changed);
      widget.controller.addListener(_changed);
    }
  }

  void _changed() {
    if (mounted) setState(() {});
  }

  @override
  void dispose() {
    widget.controller.removeListener(_changed);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final controller = widget.controller;
    if (!controller.initialized || controller.loading) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    if (controller.error != null && controller.hasCredential) {
      return _IdentityLoadError(
        message: controller.error!,
        onRetry: controller.retry,
        onReset: controller.clearLocalCredential,
      );
    }
    if (widget.connectHouseholdOnStart) {
      return HouseholdOnboarding(controller: controller);
    }
    if (!controller.hasCredential) {
      return GoogleIdentityPrompt(controller: controller);
    }
    if (controller.households.isEmpty) {
      return HouseholdOnboarding(controller: controller);
    }
    return widget.authenticatedHomeBuilder(context, controller);
  }
}

class GoogleIdentityPrompt extends StatelessWidget {
  const GoogleIdentityPrompt({super.key, required this.controller});

  final IdentityController controller;

  @override
  Widget build(BuildContext context) => Scaffold(
    body: SafeArea(
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.cloud_outlined, size: 44),
              const SizedBox(height: 12),
              Text(
                '連結雲端家庭',
                style: Theme.of(context).textTheme.titleLarge,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(
                controller.canSignInWithGoogle
                    ? '使用 Google 登入，將此裝置連結至家庭。'
                    : '此版本尚未設定 Google 登入。',
                textAlign: TextAlign.center,
              ),
              if (controller.error != null) ...[
                const SizedBox(height: 12),
                Text(
                  controller.error!,
                  textAlign: TextAlign.center,
                  style: TextStyle(color: Theme.of(context).colorScheme.error),
                ),
              ],
              const SizedBox(height: 20),
              FilledButton.icon(
                onPressed: !controller.canSignInWithGoogle || controller.loading
                    ? null
                    : controller.signInWithGoogle,
                icon: const Icon(Icons.login),
                label: controller.loading
                    ? const SizedBox.square(
                        dimension: 20,
                        child: CircularProgressIndicator(strokeWidth: 2),
                      )
                    : Text(
                        controller.error == null
                            ? '使用 Google 繼續'
                            : '使用 Google 重試',
                      ),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

class _IdentityLoadError extends StatelessWidget {
  const _IdentityLoadError({
    required this.message,
    required this.onRetry,
    required this.onReset,
  });

  final String message;
  final Future<void> Function() onRetry;
  final Future<void> Function() onReset;

  @override
  Widget build(BuildContext context) => Scaffold(
    body: Center(
      child: Padding(
        padding: const EdgeInsets.all(24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            const Icon(Icons.cloud_off_outlined, size: 40),
            const SizedBox(height: 12),
            Text(message, textAlign: TextAlign.center),
            const SizedBox(height: 16),
            FilledButton(onPressed: onRetry, child: const Text('重試')),
            TextButton(
              onPressed: () async {
                final confirmed = await _confirm(
                  context,
                  title: '清除此裝置的登入資料？',
                  message:
                      '此裝置儲存的登入憑證將會移除。'
                      '如要重新連結，請使用管理員提供的設定碼。',
                );
                if (confirmed) await onReset();
              },
              child: const Text('清除本機登入資料'),
            ),
          ],
        ),
      ),
    ),
  );
}

enum _OnboardingMode { create, join }

class HouseholdOnboarding extends StatefulWidget {
  const HouseholdOnboarding({super.key, required this.controller});
  final IdentityController controller;

  @override
  State<HouseholdOnboarding> createState() => _HouseholdOnboardingState();
}

class _HouseholdOnboardingState extends State<HouseholdOnboarding> {
  final _challenge = TextEditingController();
  final _oneTimeCode = TextEditingController();
  _OnboardingMode _mode = _OnboardingMode.create;
  bool _busy = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_controllerChanged);
  }

  @override
  void didUpdateWidget(covariant HouseholdOnboarding oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_controllerChanged);
      widget.controller.addListener(_controllerChanged);
    }
    _controllerChanged();
  }

  void _controllerChanged() {
    _clearUsedChallenge();
    if (mounted) setState(() {});
  }

  void _clearUsedChallenge() {
    if (widget.controller.hasCredential && _challenge.text.isNotEmpty) {
      _challenge.clear();
    }
  }

  String get _actionLabel => switch (_mode) {
    _OnboardingMode.create => '建立家庭',
    _OnboardingMode.join => '使用邀請碼加入',
  };

  Future<void> _submit() async {
    if (_busy) return;
    FocusScope.of(context).unfocus();
    setState(() {
      _busy = true;
      _error = null;
    });
    final challenge = _challenge.text;
    final code = _oneTimeCode.text.trim();
    try {
      if (!widget.controller.hasCredential) {
        switch (_mode) {
          case _OnboardingMode.create:
            await widget.controller.createWithChallenge(challenge);
          case _OnboardingMode.join:
            await widget.controller.joinWithChallenge(challenge, code);
        }
      } else {
        switch (_mode) {
          case _OnboardingMode.create:
            await widget.controller.createHousehold();
          case _OnboardingMode.join:
            await widget.controller.joinHousehold(code);
        }
      }
      _challenge.clear();
      _oneTimeCode.clear();
    } catch (error) {
      if (mounted) setState(() => _error = _errorMessage(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  String _errorMessage(Object error) => error is IdentityHttpFailure
      ? error.message
      : '無法完成操作，請確認設定碼或邀請碼後再試。';

  @override
  void dispose() {
    widget.controller.removeListener(_controllerChanged);
    _challenge.dispose();
    _oneTimeCode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (widget.controller.hasCredential) return _buildHouseholdAccess(context);
    return _buildSetup(context);
  }

  Widget _buildSetup(BuildContext context) {
    final needsBootstrap = !widget.controller.hasCredential;
    return _ConnectRoom(
      child: ListView(
        padding: const EdgeInsets.all(24),
        children: [
          const _ConnectIllustration(),
          const SizedBox(height: 12),
          _paper(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                const SizedBox(height: 12),
                Text(
                  '選擇此裝置連結家庭的方式。',
                  style: Theme.of(context).textTheme.titleLarge,
                  textAlign: TextAlign.center,
                ),
                if (widget.controller.canSignInWithGoogle) ...[
                  const SizedBox(height: 16),
                  FilledButton.icon(
                    key: const Key('connect-google-sign-in'),
                    onPressed: widget.controller.loading
                        ? null
                        : widget.controller.signInWithGoogle,
                    icon: const Icon(Icons.login),
                    label: Text(
                      widget.controller.error == null
                          ? '使用 Google 繼續'
                          : '使用 Google 重試',
                    ),
                  ),
                  if (widget.controller.error != null) ...[
                    const SizedBox(height: 8),
                    Text(
                      widget.controller.error!,
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: Theme.of(context).colorScheme.error,
                      ),
                    ),
                  ],
                ],
                if (!widget.controller.apiHostConfigured) ...[
                  const SizedBox(height: 12),
                  Text(
                    '連結前請先設定 API_BASE_URL。'
                    'MEDIA_API_BASE 用於設定媒體服務端點。',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                  ),
                ],
                const SizedBox(height: 20),
                Wrap(
                  alignment: WrapAlignment.center,
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _modeChip(_OnboardingMode.create, '建立家庭'),
                    _modeChip(_OnboardingMode.join, '加入家庭'),
                  ],
                ),
                if (needsBootstrap) ...[
                  const SizedBox(height: 20),
                  TextField(
                    controller: _challenge,
                    obscureText: true,
                    autocorrect: false,
                    enableSuggestions: false,
                    decoration: const InputDecoration(
                      labelText: '管理員提供的一次性設定碼',
                      helperText:
                          '請向管理員取得。設定碼僅供使用一次，App 不會儲存。',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ],
                if (_mode == _OnboardingMode.join) ...[
                  const SizedBox(height: 16),
                  TextField(
                    controller: _oneTimeCode,
                    obscureText: true,
                    autocorrect: false,
                    enableSuggestions: false,
                    decoration: InputDecoration(
                      labelText: '家庭邀請碼',
                      helperText: '邀請碼僅供使用一次，App 不會儲存。',
                      border: const OutlineInputBorder(),
                    ),
                  ),
                ],
                if (_error != null) ...[
                  const SizedBox(height: 12),
                  Text(
                    _error!,
                    style: TextStyle(
                      color: Theme.of(context).colorScheme.error,
                    ),
                    textAlign: TextAlign.center,
                  ),
                ],
                const SizedBox(height: 20),
                FilledButton(
                  onPressed: _busy || !widget.controller.apiHostConfigured
                      ? null
                      : _submit,
                  child: _busy
                      ? const SizedBox.square(
                          dimension: 20,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : Text(_actionLabel),
                ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildHouseholdAccess(BuildContext context) {
    final controller = widget.controller;
    final memberships = controller.households;
    final selected = controller.selectedHousehold;
    final canManage = selected?.canManageMembers ?? false;
    return _ConnectRoom(
      child: ListView(
        key: const Key('connect-household-page'),
        padding: const EdgeInsets.fromLTRB(20, 12, 20, 32),
        children: [
          const _ConnectIllustration(),
          const SizedBox(height: 8),
          _paper(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  '我的家庭',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 6),
                const Text(
                  '切換家庭會變更此裝置目前使用的家庭；建立或加入家庭不會影響其他家庭成員資格。',
                ),
                const SizedBox(height: 14),
                if (memberships.isEmpty)
                  const Text('尚未加入任何家庭。')
                else
                  DropdownButtonFormField<String>(
                    key: const Key('connect-household-selector'),
                    initialValue: controller.selectedHouseholdId,
                    isExpanded: true,
                    decoration: const InputDecoration(
                      labelText: '目前使用的家庭',
                    ),
                    items: memberships
                        .map(
                          (membership) => DropdownMenuItem(
                            value: membership.householdId,
                            child: Text(
                              '${membership.householdId} · ${_roleLabel(membership.role)}',
                              overflow: TextOverflow.ellipsis,
                            ),
                          ),
                        )
                        .toList(growable: false),
                    onChanged: _busy
                        ? null
                        : (id) {
                            if (id != null) controller.selectHousehold(id);
                          },
                  ),
                const SizedBox(height: 16),
                Wrap(
                  spacing: 10,
                  runSpacing: 10,
                  children: [
                    FilledButton.tonalIcon(
                      key: const Key('connect-create-household'),
                      onPressed: _busy ? null : _createAnotherHousehold,
                      icon: const Icon(Icons.add_home_outlined),
                      label: const Text('建立家庭'),
                    ),
                    OutlinedButton.icon(
                      key: const Key('connect-redeem-invite'),
                      onPressed: _busy ? null : _redeemInvite,
                      icon: const Icon(Icons.key_outlined),
                      label: const Text('使用邀請碼加入'),
                    ),
                  ],
                ),
              ],
            ),
          ),
          const SizedBox(height: 14),
          _paper(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                Text(
                  '家庭邀請',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 8),
                if (selected == null)
                  const Text(
                    '請先選擇家庭。',
                  )
                else ...[
                  Text('目前角色：${_roleLabel(selected.role)}'),
                  const SizedBox(height: 12),
                  if (canManage)
                    _actionButton(
                      key: const Key('connect-issue-invite'),
                      label: '分享家庭邀請碼',
                      icon: Icons.ios_share_outlined,
                      onPressed: () => _issueCode(
                        controller.createInvite,
                        title: '家庭邀請碼',
                      ),
                    ),
                ],
                if (_busy)
                  const Padding(
                    padding: EdgeInsets.all(8),
                    child: Center(
                      child: CircularProgressIndicator(
                        key: Key('connect-busy'),
                      ),
                    ),
                  ),
                if (_error != null)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Semantics(
                      liveRegion: true,
                      child: Text(
                        _error!,
                        key: const Key('connect-error'),
                        style: TextStyle(
                          color: Theme.of(context).colorScheme.error,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                  ),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await action();
    } catch (error) {
      if (mounted) setState(() => _error = _errorMessage(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _createAnotherHousehold() =>
      _run(() => widget.controller.createHousehold());

  Future<void> _redeemInvite() async {
    final code = await _askForValue(
      title: '加入家庭',
      label: '家庭邀請碼',
      obscure: true,
    );
    if (code != null) {
      await _run(() => widget.controller.joinHousehold(code));
    }
  }

  Future<String?> _askForValue({
    required String title,
    required String label,
    bool obscure = false,
  }) async {
    final value = await showDialog<String>(
      context: context,
      builder: (context) => _ValueEntryDialog(
        title: title,
        label: label,
        obscure: obscure,
        submitLabel: '確認加入',
      ),
    );
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> _issueCode(
    Future<IssuedOneTimeCode> Function() issue, {
    required String title,
  }) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final code = await issue();
      if (!mounted) return;
      setState(() => _busy = false);
      await showDialog<void>(
        context: context,
        builder: (context) => _IssuedCodeDialog(
          title: title,
          code: code,
          warning: '邀請碼僅顯示一次，請複製後分享給要加入的家庭成員。',
        ),
      );
    } catch (error) {
      if (mounted) setState(() => _error = _errorMessage(error));
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Widget _actionButton({
    required Key key,
    required String label,
    required IconData icon,
    required VoidCallback onPressed,
  }) => Padding(
    padding: const EdgeInsets.only(bottom: 8),
    child: OutlinedButton.icon(
      key: key,
      onPressed: _busy ? null : onPressed,
      icon: Icon(icon),
      label: Text(label),
      style: OutlinedButton.styleFrom(alignment: Alignment.centerLeft),
    ),
  );

  Widget _modeChip(_OnboardingMode mode, String label) => ChoiceChip(
    key: Key('onboarding-mode-${mode.name}'),
    label: Text(label),
    selected: _mode == mode,
    onSelected: _busy ? null : (_) => setState(() => _mode = mode),
  );
}

String _roleLabel(HouseholdRole role) => switch (role) {
  HouseholdRole.owner => '擁有者',
  HouseholdRole.admin => '管理員',
  HouseholdRole.member => '成員',
};

Widget _paper({required Widget child}) => HandDrawnCardShell(child: child);

class _ConnectRoom extends StatelessWidget {
  const _ConnectRoom({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: Colors.transparent,
    extendBodyBehindAppBar: true,
    appBar: AppBar(
      title: const Text('連結家庭'),
      backgroundColor: Colors.transparent,
      surfaceTintColor: Colors.transparent,
      elevation: 0,
    ),
    body: Stack(
      children: [
        const Positioned.fill(child: ColoredBox(color: Color(0xFFEAEDF5))),
        Positioned.fill(
          child: Image.asset(
            'assets/images/room/background.png',
            fit: BoxFit.contain,
            alignment: Alignment.center,
            excludeFromSemantics: true,
          ),
        ),
        SafeArea(
          top: false,
          child: LayoutBuilder(
            builder: (context, constraints) => Align(
              alignment: Alignment.topCenter,
              child: SizedBox(
                width: constraints.maxWidth > 700 ? 700 : constraints.maxWidth,
                height: constraints.maxHeight,
                child: Padding(
                  padding: EdgeInsets.only(
                    top: kToolbarHeight + MediaQuery.paddingOf(context).top,
                  ),
                  child: child,
                ),
              ),
            ),
          ),
        ),
      ],
    ),
  );
}

class _ConnectIllustration extends StatelessWidget {
  const _ConnectIllustration();

  @override
  Widget build(BuildContext context) => Column(
    children: [
      SizedBox(
        height: 104,
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            Image.asset(
              'assets/images/agent/noki_original.png',
              width: 86,
              height: 96,
              fit: BoxFit.contain,
              excludeFromSemantics: true,
            ),
            SizedBox(
              width: 208,
              height: 96,
              child: Stack(
                alignment: Alignment.center,
                children: [
                  Image.asset(
                    'assets/images/ui/thought_bubble.png',
                    fit: BoxFit.fill,
                    excludeFromSemantics: true,
                  ),
                  const Padding(
                    padding: EdgeInsets.fromLTRB(34, 20, 30, 18),
                    child: Text(
                      '一起把家的日常照顧好！',
                      textAlign: TextAlign.center,
                      style: TextStyle(
                        color: AppTokens.darkBlue,
                        fontWeight: FontWeight.w700,
                      ),
                    ),
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
      Text(
        '讓每個家庭都有安心的小窩',
        textAlign: TextAlign.center,
        style: Theme.of(context).textTheme.titleMedium
            ?.copyWith(color: AppTokens.darkBlue, fontWeight: FontWeight.w700),
      ),
    ],
  );
}

class _IssuedCodeDialog extends StatefulWidget {
  const _IssuedCodeDialog({
    required this.title,
    required this.code,
    required this.warning,
  });

  final String title;
  final IssuedOneTimeCode code;
  final String warning;

  @override
  State<_IssuedCodeDialog> createState() => _IssuedCodeDialogState();
}

class _IssuedCodeDialogState extends State<_IssuedCodeDialog> {
  bool _copied = false;

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text(widget.title),
    content: Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        SelectableText(
          widget.code.value,
          key: const Key('issued-code-value'),
          style: Theme.of(context).textTheme.titleMedium
              ?.copyWith(fontWeight: FontWeight.w700, letterSpacing: 1),
        ),
        if (widget.code.expiresAt != null) ...[
          const SizedBox(height: 8),
          Text('有效期限：${widget.code.expiresAt!.toLocal()}'),
        ],
        const SizedBox(height: 10),
        Text(widget.warning),
      ],
    ),
    actions: [
      TextButton.icon(
        key: const Key('copy-issued-code'),
        onPressed: () async {
          await Clipboard.setData(ClipboardData(text: widget.code.value));
          if (mounted) setState(() => _copied = true);
        },
        icon: Icon(_copied ? Icons.check : Icons.copy),
        label: Text(_copied ? '已複製' : '複製邀請碼'),
      ),
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('完成'),
      ),
    ],
  );
}

class _ValueEntryDialog extends StatefulWidget {
  const _ValueEntryDialog({
    required this.title,
    required this.label,
    required this.submitLabel,
    this.obscure = false,
  });

  final String title;
  final String label;
  final String submitLabel;
  final bool obscure;

  @override
  State<_ValueEntryDialog> createState() => _ValueEntryDialogState();
}

class _ValueEntryDialogState extends State<_ValueEntryDialog> {
  final _input = TextEditingController();

  @override
  void dispose() {
    _input.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => AlertDialog(
    title: Text(widget.title),
    content: TextField(
      controller: _input,
      autofocus: true,
      obscureText: widget.obscure,
      autocorrect: false,
      enableSuggestions: false,
      decoration: InputDecoration(labelText: widget.label),
    ),
    actions: [
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('取消'),
      ),
      FilledButton(
        onPressed: () => Navigator.pop(context, _input.text.trim()),
        child: Text(widget.submitLabel),
      ),
    ],
  );
}

class HouseholdSwitcher extends StatelessWidget {
  const HouseholdSwitcher({
    super.key,
    required this.controller,
    required this.onManage,
  });

  final IdentityController controller;
  final VoidCallback onManage;

  @override
  Widget build(BuildContext context) => AnimatedBuilder(
    animation: controller,
    builder: (context, _) {
      final selectedId = controller.selectedHouseholdId;
      if (selectedId == null) return const SizedBox.shrink();
      return Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          DropdownButtonHideUnderline(
            child: DropdownButton<String>(
              key: const Key('household-switcher'),
              value: selectedId,
              icon: const Icon(Icons.swap_horiz),
              items: controller.households
                  .map(
                    (household) => DropdownMenuItem(
                      value: household.householdId,
                      child: ConstrainedBox(
                        constraints: const BoxConstraints(maxWidth: 150),
                        child: Text(
                          household.householdId,
                          overflow: TextOverflow.ellipsis,
                        ),
                      ),
                    ),
                  )
                  .toList(growable: false),
              onChanged: (id) {
                if (id != null) controller.selectHousehold(id);
              },
            ),
          ),
          IconButton(
            tooltip: 'Household access and recovery',
            onPressed: onManage,
            icon: const Icon(Icons.manage_accounts_outlined),
          ),
        ],
      );
    },
  );
}

Future<void> showHouseholdManagement(
  BuildContext context,
  IdentityController controller,
) => showModalBottomSheet<void>(
  context: context,
  isScrollControlled: true,
  builder: (context) => HouseholdManagementSheet(controller: controller),
);

class HouseholdManagementSheet extends StatefulWidget {
  const HouseholdManagementSheet({super.key, required this.controller});
  final IdentityController controller;

  @override
  State<HouseholdManagementSheet> createState() =>
      _HouseholdManagementSheetState();
}

class _HouseholdManagementSheetState extends State<HouseholdManagementSheet> {
  bool _busy = false;
  String? _feedback;

  Future<void> _run(Future<void> Function() action, {String? success}) async {
    setState(() {
      _busy = true;
      _feedback = null;
    });
    try {
      await action();
      if (mounted && success != null) setState(() => _feedback = success);
    } catch (error) {
      if (mounted) {
        setState(
          () => _feedback = error is IdentityHttpFailure
              ? error.message
              : 'Action failed. Check access and try again.',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _issueCode(
    Future<IssuedOneTimeCode> Function() issue, {
    required String title,
  }) async {
    setState(() => _busy = true);
    try {
      final code = await issue();
      if (!mounted) return;
      await showDialog<void>(
        context: context,
        builder: (context) => AlertDialog(
          title: Text(title),
          content: SelectableText(
            '${code.value}'
            '${code.expiresAt == null ? '' : '\nExpires: ${code.expiresAt!.toLocal()}'}'
            '\n\nThis code is shown once. Copy it to a safe place; '
            'it is not stored by the app.',
          ),
          actions: [
            TextButton(
              onPressed: () => Navigator.pop(context),
              child: const Text('Done'),
            ),
          ],
        ),
      );
    } catch (error) {
      if (mounted) {
        setState(
          () => _feedback = error is IdentityHttpFailure
              ? error.message
              : 'Could not issue the one-time code.',
        );
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<String?> _askForValue({
    required String title,
    required String label,
    bool obscure = false,
  }) async {
    final input = TextEditingController();
    final value = await showDialog<String>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: TextField(
          controller: input,
          autofocus: true,
          obscureText: obscure,
          autocorrect: false,
          enableSuggestions: false,
          decoration: InputDecoration(labelText: label),
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, input.text.trim()),
            child: const Text('Continue'),
          ),
        ],
      ),
    );
    input.dispose();
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> _join() async {
    final code = await _askForValue(
      title: 'Join household',
      label: 'One-time invite code',
      obscure: true,
    );
    if (code != null) {
      await _run(
        () => widget.controller.joinHousehold(code),
        success: 'Household joined.',
      );
    }
  }

  Future<void> _revokeMembership() async {
    final deviceId = await _askForValue(
      title: 'Revoke household membership',
      label: 'Target device ID',
    );
    if (deviceId != null) {
      await _run(
        () => widget.controller.revokeMembership(deviceId),
        success: 'Membership revoked for this household.',
      );
    }
  }

  Future<void> _redeemRecovery({required bool household}) async {
    final code = await _askForValue(
      title: household
          ? 'Recover household owner access'
          : 'Revoke old install',
      label: household ? 'Household recovery code' : 'Device recovery code',
      obscure: true,
    );
    if (code == null) return;
    await _run(
      () => household
          ? widget.controller.redeemHouseholdRecoveryCode(code)
          : widget.controller.redeemDeviceRecoveryCode(code),
      success: household
          ? 'Household owner access restored.'
          : 'Old install and its memberships revoked.',
    );
  }

  @override
  Widget build(BuildContext context) {
    final household = widget.controller.selectedHousehold;
    final canManage = household?.canManageMembers ?? false;
    final isOwner = household?.role == HouseholdRole.owner;
    return SafeArea(
      child: Padding(
        padding: EdgeInsets.fromLTRB(
          20,
          16,
          20,
          20 + MediaQuery.viewInsetsOf(context).bottom,
        ),
        child: SingleChildScrollView(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                'Household access',
                style: Theme.of(context).textTheme.titleLarge,
              ),
              const SizedBox(height: 4),
              Text(
                '${household?.householdId ?? 'No household'}'
                '${household == null ? '' : ' · ${household.role.name}'}',
              ),
              const SizedBox(height: 12),
              if (_feedback != null) ...[
                Text(_feedback!, textAlign: TextAlign.center),
                const SizedBox(height: 8),
              ],
              FilledButton.tonal(
                onPressed: _busy
                    ? null
                    : () => _run(
                        widget.controller.createHousehold,
                        success: 'New household created.',
                      ),
                child: const Text('Create another household'),
              ),
              OutlinedButton(
                onPressed: _busy ? null : _join,
                child: const Text('Join with invite code'),
              ),
              if (canManage)
                OutlinedButton(
                  onPressed: _busy
                      ? null
                      : () => _issueCode(
                          widget.controller.createInvite,
                          title: 'One-time household invite',
                        ),
                  child: const Text('Issue member invite'),
                ),
              if (canManage)
                OutlinedButton(
                  onPressed: _busy ? null : _revokeMembership,
                  child: const Text('Revoke member device in this household'),
                ),
              if (isOwner)
                OutlinedButton(
                  onPressed: _busy
                      ? null
                      : () => _issueCode(
                          widget.controller.issueHouseholdRecoveryCode,
                          title: 'One-time household recovery code',
                        ),
                  child: const Text('Issue household recovery code'),
                ),
              OutlinedButton(
                onPressed: _busy
                    ? null
                    : () => _issueCode(
                        widget.controller.issueDeviceRecoveryCode,
                        title: 'One-time device recovery code',
                      ),
                child: const Text('Issue device recovery code'),
              ),
              OutlinedButton(
                onPressed: _busy
                    ? null
                    : () => _redeemRecovery(household: true),
                child: const Text('Redeem household recovery code'),
              ),
              OutlinedButton(
                onPressed: _busy
                    ? null
                    : () => _redeemRecovery(household: false),
                child: const Text('Redeem device recovery code'),
              ),
              TextButton(
                onPressed: _busy
                    ? null
                    : () async {
                        final confirmed = await _confirm(
                          context,
                          title: 'Leave this household?',
                          message:
                              'Only ${household?.householdId ?? 'the selected household'} '
                              'will be left. Other household memberships stay active.',
                        );
                        if (confirmed) {
                          await _run(
                            widget.controller.leaveSelectedHousehold,
                            success: 'Left the selected household.',
                          );
                        }
                      },
                child: const Text('Leave selected household'),
              ),
              if (_busy)
                const Padding(
                  padding: EdgeInsets.all(8),
                  child: Center(child: CircularProgressIndicator()),
                ),
            ],
          ),
        ),
      ),
    );
  }
}

Future<bool> _confirm(
  BuildContext context, {
  required String title,
  required String message,
}) async =>
    await showDialog<bool>(
      context: context,
      builder: (context) => AlertDialog(
        title: Text(title),
        content: Text(message),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(context, false),
            child: const Text('Cancel'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(context, true),
            child: const Text('Confirm'),
          ),
        ],
      ),
    ) ??
    false;
