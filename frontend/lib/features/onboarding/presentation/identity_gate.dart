import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../data/identity_api.dart';
import '../domain/identity_controller.dart';
import '../domain/identity_models.dart';
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
                'Connect to your cloud household',
                style: Theme.of(context).textTheme.titleLarge,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 8),
              Text(
                controller.canSignInWithGoogle
                    ? 'Sign in with Google to connect this install to your household.'
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
                    : Text(
                        controller.error == null
                            ? 'Continue with Google'
                            : 'Retry with Google',
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
                  'Choose how this install gets household access.',
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
                          ? 'Continue with Google'
                          : 'Retry with Google',
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
                    'Configure API_BASE_URL at build time before connecting. '
                    'MEDIA_API_BASE separately selects the media endpoint.',
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
                    _modeChip(_OnboardingMode.create, 'Create household'),
                    _modeChip(_OnboardingMode.join, 'Join household'),
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
        ],
      ),
    );
  }

  Widget _buildHouseholdAccess(BuildContext context) {
    final controller = widget.controller;
    final memberships = controller.households;
    final selected = controller.selectedHousehold;
    final canManage = selected?.canManageMembers ?? false;
    final isOwner = selected?.role == HouseholdRole.owner;
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
                  'Your households',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 6),
                const Text(
                  'Switching changes the active household for this install. '
                  'Creating or joining another household keeps your other memberships.',
                ),
                const SizedBox(height: 14),
                if (memberships.isEmpty)
                  const Text('No household memberships yet.')
                else
                  DropdownButtonFormField<String>(
                    key: const Key('connect-household-selector'),
                    initialValue: controller.selectedHouseholdId,
                    isExpanded: true,
                    decoration: const InputDecoration(
                      labelText: 'Active household',
                    ),
                    items: memberships
                        .map(
                          (membership) => DropdownMenuItem(
                            value: membership.householdId,
                            child: Text(
                              '${membership.householdId} · ${membership.role.name}',
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
                      label: const Text('Create household'),
                    ),
                    OutlinedButton.icon(
                      key: const Key('connect-redeem-invite'),
                      onPressed: _busy ? null : _redeemInvite,
                      icon: const Icon(Icons.key_outlined),
                      label: const Text('Redeem invite code'),
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
                  'Household access',
                  style: Theme.of(context).textTheme.titleLarge,
                ),
                const SizedBox(height: 8),
                if (selected == null)
                  const Text(
                    'Select a household to see available access actions.',
                  )
                else ...[
                  Text('Current role: ${selected.role.name}'),
                  const SizedBox(height: 12),
                  if (canManage)
                    _actionButton(
                      key: const Key('connect-issue-invite'),
                      label: 'Issue invite code',
                      icon: Icons.ios_share_outlined,
                      onPressed: () => _issueCode(
                        controller.createInvite,
                        title: 'One-time household invite',
                      ),
                    ),
                  if (isOwner)
                    _actionButton(
                      key: const Key('connect-issue-household-recovery'),
                      label: 'Issue household recovery code',
                      icon: Icons.shield_outlined,
                      onPressed: () => _issueCode(
                        controller.issueHouseholdRecoveryCode,
                        title: 'One-time household recovery code',
                      ),
                    ),
                  if (canManage)
                    OutlinedButton.icon(
                      key: const Key('connect-revoke-member'),
                      onPressed: _busy ? null : _revokeMembership,
                      icon: const Icon(Icons.person_remove_outlined),
                      label: const Text('Revoke member device'),
                    ),
                  TextButton.icon(
                    key: const Key('connect-leave-household'),
                    onPressed: _busy ? null : _leaveSelectedHousehold,
                    icon: const Icon(Icons.logout),
                    label: const Text('Leave selected household'),
                  ),
                ],
                _actionButton(
                  key: const Key('connect-issue-device-recovery'),
                  label: 'Issue device recovery code',
                  icon: Icons.phone_android_outlined,
                  onPressed: () => _issueCode(
                    controller.issueDeviceRecoveryCode,
                    title: 'One-time device recovery code',
                    deviceRecovery: true,
                  ),
                ),
                const Text(
                  'Device recovery replaces the old install credential and '
                  'revokes its access. It does not migrate household data.',
                ),
                const SizedBox(height: 4),
                OutlinedButton.icon(
                  key: const Key('connect-redeem-household-recovery'),
                  onPressed: _busy
                      ? null
                      : () => _redeemRecovery(household: true),
                  icon: const Icon(Icons.restore_outlined),
                  label: const Text('Redeem household recovery code'),
                ),
                OutlinedButton.icon(
                  key: const Key('connect-redeem-device-recovery'),
                  onPressed: _busy
                      ? null
                      : () => _redeemRecovery(household: false),
                  icon: const Icon(Icons.phonelink_erase_outlined),
                  label: const Text('Redeem device recovery code'),
                ),
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
      title: 'Redeem household invite',
      label: 'One-time invite code',
      obscure: true,
    );
    if (code != null) {
      await _run(() => widget.controller.joinHousehold(code));
    }
  }

  Future<void> _revokeMembership() async {
    final deviceId = await _askForValue(
      title: 'Revoke household member',
      label: 'Target device ID',
    );
    if (deviceId != null) {
      await _run(() => widget.controller.revokeMembership(deviceId));
    }
  }

  Future<void> _leaveSelectedHousehold() async {
    final household = widget.controller.selectedHousehold;
    if (household == null) return;
    final confirmed = await _confirm(
      context,
      title: 'Leave this household?',
      message:
          'Only ${household.householdId} will be left. Your other household '
          'memberships stay active.',
    );
    if (confirmed) await _run(widget.controller.leaveSelectedHousehold);
  }

  Future<void> _redeemRecovery({required bool household}) async {
    final code = await _askForValue(
      title: household
          ? 'Redeem household recovery code'
          : 'Redeem device recovery code',
      label: household ? 'Household recovery code' : 'Device recovery code',
      obscure: true,
    );
    if (code == null) return;
    await _run(
      () => household
          ? widget.controller.redeemHouseholdRecoveryCode(code)
          : widget.controller.redeemDeviceRecoveryCode(code),
    );
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
        submitLabel: 'Redeem',
      ),
    );
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> _issueCode(
    Future<IssuedOneTimeCode> Function() issue, {
    required String title,
    bool deviceRecovery = false,
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
          warning: deviceRecovery
              ? 'Using this code revokes the old install and its access. '
                    'It does not migrate household data.'
              : 'This one-time code is shown once. Save it somewhere safe.',
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

Widget _paper({required Widget child}) => Container(
  padding: const EdgeInsets.all(18),
  decoration: BoxDecoration(
    color: AppTokens.card.withValues(alpha: .96),
    border: Border.all(color: AppTokens.border, width: 1.4),
    borderRadius: const BorderRadius.all(Radius.circular(22)),
    boxShadow: const [
      BoxShadow(color: Color(0x1F252A38), blurRadius: 8, offset: Offset(0, 4)),
    ],
  ),
  child: child,
);

class _ConnectRoom extends StatelessWidget {
  const _ConnectRoom({required this.child});
  final Widget child;

  @override
  Widget build(BuildContext context) => Scaffold(
    backgroundColor: Colors.transparent,
    extendBodyBehindAppBar: true,
    appBar: AppBar(
      title: const Text('Connect a household'),
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
      const SizedBox(
        height: 82,
        width: 112,
        child: CustomPaint(painter: _HouseDoodlePainter()),
      ),
      Text(
        'A little room for every household',
        textAlign: TextAlign.center,
        style: Theme.of(context).textTheme.titleMedium
            ?.copyWith(color: AppTokens.darkBlue, fontWeight: FontWeight.w700),
      ),
    ],
  );
}

class _HouseDoodlePainter extends CustomPainter {
  const _HouseDoodlePainter();

  @override
  void paint(Canvas canvas, Size size) {
    final pen = Paint()
      ..color = AppTokens.darkBlue
      ..style = PaintingStyle.stroke
      ..strokeWidth = 3
      ..strokeCap = StrokeCap.round
      ..strokeJoin = StrokeJoin.round;
    final house = Path()
      ..moveTo(size.width * .19, size.height * .45)
      ..lineTo(size.width * .5, size.height * .16)
      ..lineTo(size.width * .81, size.height * .45)
      ..lineTo(size.width * .77, size.height * .47)
      ..lineTo(size.width * .77, size.height * .84)
      ..lineTo(size.width * .24, size.height * .84)
      ..lineTo(size.width * .24, size.height * .47)
      ..close();
    canvas.drawPath(house, pen);
    final door = Path()
      ..moveTo(size.width * .44, size.height * .83)
      ..lineTo(size.width * .44, size.height * .59)
      ..quadraticBezierTo(
        size.width * .5,
        size.height * .52,
        size.width * .56,
        size.height * .59,
      )
      ..lineTo(size.width * .56, size.height * .83);
    canvas.drawPath(door, pen);
    final flourish = Path()
      ..moveTo(size.width * .12, size.height * .3)
      ..quadraticBezierTo(
        size.width * .08,
        size.height * .12,
        size.width * .28,
        size.height * .12,
      )
      ..moveTo(size.width * .82, size.height * .24)
      ..quadraticBezierTo(
        size.width * .96,
        size.height * .16,
        size.width * .9,
        size.height * .39,
      );
    canvas.drawPath(flourish, pen);
  }

  @override
  bool shouldRepaint(covariant _HouseDoodlePainter oldDelegate) => false;
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
          Text('Expires: ${widget.code.expiresAt!.toLocal()}'),
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
        label: Text(_copied ? 'Copied' : 'Copy code'),
      ),
      TextButton(
        onPressed: () => Navigator.pop(context),
        child: const Text('Done'),
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
        child: const Text('Cancel'),
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
