import 'package:flutter/material.dart';

import '../data/identity_api.dart';
import '../domain/identity_controller.dart';
import '../domain/identity_models.dart';

typedef AuthenticatedHomeBuilder = Widget Function(
  BuildContext context,
  IdentityController controller,
);

class IdentityGate extends StatefulWidget {
  const IdentityGate({
    super.key,
    required this.controller,
    required this.authenticatedHomeBuilder,
  });

  final IdentityController controller;
  final AuthenticatedHomeBuilder authenticatedHomeBuilder;

  @override
  State<IdentityGate> createState() => _IdentityGateState();
}

class _IdentityGateState extends State<IdentityGate> {
  @override
  void initState() {
    super.initState();
    widget.controller.addListener(_changed);
    if (!widget.controller.initialized) {
      widget.controller.initialize();
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
    if (!controller.hasCredential || controller.households.isEmpty) {
      return GoogleIdentityPrompt(controller: controller);
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
                'Connect to your cloud household',
                style: Theme.of(context).textTheme.titleLarge,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(
                controller.canSignInWithGoogle
                    ? 'Sign in with the same Google account to restore your household.'
                    : 'Google sign-in is not configured for this build.',
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
                    : const Text('Continue with Google'),
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
            FilledButton(onPressed: onRetry, child: const Text('Retry')),
            TextButton(
              onPressed: () async {
                final confirmed = await _confirm(
                  context,
                  title: 'Clear this install login?',
                  message:
                      'The saved credential will be removed from this device. '
                      'Use an operator challenge or recovery code to connect again.',
                );
                if (confirmed) await onReset();
              },
              child: const Text('Clear local login'),
            ),
          ],
        ),
      ),
    ),
  );
}

enum _OnboardingMode { create, join, householdRecovery, deviceRecovery }

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
    widget.controller.addListener(_clearUsedChallenge);
  }

  @override
  void didUpdateWidget(covariant HouseholdOnboarding oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.controller != widget.controller) {
      oldWidget.controller.removeListener(_clearUsedChallenge);
      widget.controller.addListener(_clearUsedChallenge);
    }
    _clearUsedChallenge();
  }

  void _clearUsedChallenge() {
    if (widget.controller.hasCredential && _challenge.text.isNotEmpty) {
      _challenge.clear();
    }
  }

  bool get _requiresCode =>
      _mode != _OnboardingMode.create && _mode != _OnboardingMode.join;

  String get _actionLabel => switch (_mode) {
    _OnboardingMode.create => 'Create household',
    _OnboardingMode.join => 'Join household',
    _OnboardingMode.householdRecovery => 'Recover household owner access',
    _OnboardingMode.deviceRecovery => 'Revoke old install and recover',
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
          case _OnboardingMode.householdRecovery:
            await widget.controller.recoverHouseholdWithChallenge(
              challenge,
              code,
            );
          case _OnboardingMode.deviceRecovery:
            await widget.controller.recoverDeviceWithChallenge(challenge, code);
        }
      } else {
        switch (_mode) {
          case _OnboardingMode.create:
            await widget.controller.createHousehold();
          case _OnboardingMode.join:
            await widget.controller.joinHousehold(code);
          case _OnboardingMode.householdRecovery:
            await widget.controller.redeemHouseholdRecoveryCode(code);
          case _OnboardingMode.deviceRecovery:
            await widget.controller.redeemDeviceRecoveryCode(code);
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
      : 'Could not complete this action. Check the code and configured API host.';

  @override
  void dispose() {
    widget.controller.removeListener(_clearUsedChallenge);
    _challenge.dispose();
    _oneTimeCode.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final needsBootstrap = !widget.controller.hasCredential;
    return Scaffold(
      appBar: AppBar(title: const Text('Connect a household')),
      body: SafeArea(
        child: Center(
          child: ConstrainedBox(
            constraints: const BoxConstraints(maxWidth: 560),
            child: ListView(
              padding: const EdgeInsets.all(24),
              shrinkWrap: true,
              children: [
                const Icon(Icons.home_work_outlined, size: 48),
                const SizedBox(height: 12),
                Text(
                  'Choose how this install gets household access.',
                  style: Theme.of(context).textTheme.titleLarge,
                  textAlign: TextAlign.center,
                ),
                if (!widget.controller.apiHostConfigured) ...[
                  const SizedBox(height: 12),
                  const Text(
                    'Configure API_BASE_URL at build time before connecting. '
                    'MEDIA_API_BASE separately selects the media endpoint.',
                    textAlign: TextAlign.center,
                  ),
                ],
                const SizedBox(height: 20),
                Wrap(
                  alignment: WrapAlignment.center,
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    _modeChip(_OnboardingMode.create, 'Create'),
                    _modeChip(_OnboardingMode.join, 'Join'),
                    _modeChip(
                      _OnboardingMode.householdRecovery,
                      'Household recovery',
                    ),
                    _modeChip(
                      _OnboardingMode.deviceRecovery,
                      'Device recovery',
                    ),
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
                      labelText: 'One-time operator setup challenge',
                      helperText:
                          'Obtain this out-of-band from the operator. '
                          'It is used once and never saved by the app.',
                      border: OutlineInputBorder(),
                    ),
                  ),
                ],
                if (_mode != _OnboardingMode.create) ...[
                  const SizedBox(height: 16),
                  TextField(
                    controller: _oneTimeCode,
                    obscureText: true,
                    autocorrect: false,
                    enableSuggestions: false,
                    decoration: InputDecoration(
                      labelText: switch (_mode) {
                        _OnboardingMode.join => 'Household invite code',
                        _OnboardingMode.householdRecovery =>
                          'Household recovery code',
                        _OnboardingMode.deviceRecovery =>
                          'Device recovery code',
                        _OnboardingMode.create => '',
                      },
                      helperText: _requiresCode
                          ? 'One-time codes are not saved by the app.'
                          : null,
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
        ),
      ),
    );
  }

  Widget _modeChip(_OnboardingMode mode, String label) => ChoiceChip(
    label: Text(label),
    selected: _mode == mode,
    onSelected: _busy ? null : (_) => setState(() => _mode = mode),
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
