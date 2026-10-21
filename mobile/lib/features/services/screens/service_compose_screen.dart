import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/widgets.dart';
import '../service_models.dart';

/// Offering a service, asking for one, or posting a job; or editing one of mine.
class ServiceComposeScreen extends ConsumerStatefulWidget {
  const ServiceComposeScreen({super.key, this.kind = ServiceKind.offering, this.editing});

  final ServiceKind kind;

  /// The post being edited. Its kind cannot change.
  final ServicePost? editing;

  @override
  ConsumerState<ServiceComposeScreen> createState() => _ServiceComposeScreenState();
}

class _ServiceComposeScreenState extends ConsumerState<ServiceComposeScreen> {
  static const _travelChoices = [1, 2, 3, 5, 10, 20, 50];

  late ServiceKind _kind;
  String? _category;
  final _title = TextEditingController();
  final _description = TextEditingController();
  final _rate = TextEditingController();
  final _availability = TextEditingController();
  final _budget = TextEditingController();
  final _payMin = TextEditingController();
  final _payMax = TextEditingController();
  int? _travelKm;
  String? _jobType;
  String? _payPeriod;
  bool _busy = false;
  String? _error;

  bool get _editing => widget.editing != null;

  @override
  void initState() {
    super.initState();
    final post = widget.editing;
    _kind = post?.kind ?? widget.kind;
    if (post != null) {
      _category = post.category;
      _title.text = post.title;
      _description.text = post.description;
      _rate.text = post.rateInfo ?? '';
      _availability.text = post.availabilityInfo ?? '';
      _budget.text = post.budgetInfo ?? '';
      _payMin.text = post.payMinKes?.toString() ?? '';
      _payMax.text = post.payMaxKes?.toString() ?? '';
      _travelKm = post.travelKm;
      _jobType = post.jobType;
      _payPeriod = post.payPeriod;
    }
    _title.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    for (final c in [_title, _description, _rate, _availability, _budget, _payMin, _payMax]) {
      c.dispose();
    }
    super.dispose();
  }

  static String? _text(TextEditingController c) => c.text.trim().isEmpty ? null : c.text.trim();
  static int? _number(TextEditingController c) => int.tryParse(c.text.replaceAll(',', '').trim());

  /// The kind's own fields only: the server refuses another kind's.
  Map<String, dynamic> _fields() => {
        'category': _category,
        'title': _title.text.trim(),
        'description': _description.text.trim(),
        'availability_info': _text(_availability),
        ...switch (_kind) {
          ServiceKind.offering => {'rate_info': _text(_rate), 'travel_km': _travelKm},
          ServiceKind.seeking => {'budget_info': _text(_budget)},
          ServiceKind.hiring => {
              'job_type': _jobType,
              'pay_min_kes': _number(_payMin),
              'pay_max_kes': _number(_payMax),
              'pay_period': _payPeriod,
            },
        },
      };

  String? _check() {
    if (_category == null) return 'Choose a category.';
    if (_title.text.trim().isEmpty) return 'Add a title.';
    final min = _number(_payMin);
    final max = _number(_payMax);
    if (min != null && max != null && min > max) return 'The lowest pay is above the highest.';
    return null;
  }

  Future<void> _submit() async {
    final problem = _check();
    if (problem != null) {
      setState(() => _error = problem);
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(serviceRepositoryProvider);
    try {
      if (_editing) {
        final updated = await repo.update(widget.editing!.id, _fields());
        if (mounted) context.pop(updated);
        return;
      }
      final post = await repo.create(_kind, Map.of(_fields())..removeWhere((_, v) => v == null));
      if (!mounted) return;
      if (post.isHeld) {
        await showDialog<void>(
          context: context,
          builder: (dialog) => AlertDialog(
            title: const Text('Waiting for a moderator'),
            content: Text(
              'Your post mentions ${post.heldFor.map((p) => '"$p"').join(', ')}. '
              'Real employers never ask job seekers to pay, so a moderator will look at it '
              'before neighbours can see it. Only you can see it for now.',
            ),
            actions: [TextButton(onPressed: () => Navigator.of(dialog).pop(), child: const Text('OK'))],
          ),
        );
      }
      if (mounted) context.go(Routes.service(post.id));
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _busy = false;
          _error = _explain(error);
        });
      }
    }
  }

  static String _explain(ApiException error) {
    if (error.details['reason'] == 'hiring_trust_threshold') {
      final days = error.details['min_account_age_days'];
      return days == null
          ? '${error.message}. You can still offer a service or ask for help.'
          : 'Accounts can post jobs once they are $days days old. '
              'You can still offer a service or ask for help.';
    }
    return error.message;
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final (titleLabel, titleHint) = switch (_kind) {
      ServiceKind.offering => ('What do you do?', "Plumber, 10 years' experience"),
      ServiceKind.seeking => ('What do you need?', 'Someone to fix a gate latch'),
      ServiceKind.hiring => ('Job title', 'Shop attendant, hardware store'),
    };

    return Scaffold(
      appBar: AppBar(title: Text(_editing ? 'Edit post' : 'New post')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          if (!_editing) ...[
            SegmentedButton<ServiceKind>(
              showSelectedIcon: false,
              segments: const [
                ButtonSegment(value: ServiceKind.offering, label: Text('I offer')),
                ButtonSegment(value: ServiceKind.seeking, label: Text('I need')),
                ButtonSegment(value: ServiceKind.hiring, label: Text("I'm hiring")),
              ],
              selected: {_kind},
              onSelectionChanged: (value) => setState(() => _kind = value.single),
            ),
            const SizedBox(height: 8),
            Text(
              switch (_kind) {
                ServiceKind.offering =>
                  'A standing profile neighbours can find. One per category; it does not expire.',
                ServiceKind.seeking => 'Ask neighbours for a hand. It stays up for 30 days.',
                ServiceKind.hiring =>
                  'A job advert, up for 30 days. Never ask applicants for money: posts that do are held.',
              },
              style: text.bodySmall,
            ),
            const SizedBox(height: 16),
          ],
          Text('Category', style: text.titleSmall),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final entry in serviceCategories.entries)
                ChoiceChip(
                  label: Text(entry.value),
                  selected: _category == entry.key,
                  onSelected: (_) => setState(() => _category = entry.key),
                ),
            ],
          ),
          const SizedBox(height: 20),
          _Field(label: titleLabel, controller: _title, hint: titleHint, maxLength: 120),
          _Field(
            label: 'Details',
            controller: _description,
            hint: switch (_kind) {
              ServiceKind.offering => 'What you do, your experience, what areas you cover',
              ServiceKind.seeking => 'What needs doing, and when',
              ServiceKind.hiring => 'The work, the hours, and what you are looking for',
            },
            maxLength: 4000,
            lines: 5,
          ),
          ...switch (_kind) {
            ServiceKind.offering => [
                _Field(label: 'Rate', controller: _rate, hint: 'KES 800 per visit', maxLength: 200),
                _Field(label: 'When you work', controller: _availability, hint: 'Weekdays from 7am', maxLength: 200),
                Text('How far will you travel?', style: text.titleSmall),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final km in _travelChoices)
                      ChoiceChip(
                        label: Text('$km km'),
                        selected: _travelKm == km,
                        onSelected: (on) => setState(() => _travelKm = on ? km : null),
                      ),
                  ],
                ),
              ],
            ServiceKind.seeking => [
                _Field(label: 'Budget', controller: _budget, hint: 'Around KES 2,000', maxLength: 200),
                _Field(label: 'When', controller: _availability, hint: 'This weekend', maxLength: 200),
              ],
            ServiceKind.hiring => [
                Text('Type of job', style: text.titleSmall),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final entry in jobTypes.entries)
                      ChoiceChip(
                        label: Text(entry.value),
                        selected: _jobType == entry.key,
                        onSelected: (on) => setState(() => _jobType = on ? entry.key : null),
                      ),
                  ],
                ),
                const SizedBox(height: 20),
                Text('Pay (KES)', style: text.titleSmall),
                const SizedBox(height: 8),
                Row(
                  children: [
                    Expanded(child: _NumberField(controller: _payMin, hint: 'From')),
                    const SizedBox(width: 12),
                    Expanded(child: _NumberField(controller: _payMax, hint: 'To')),
                  ],
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    for (final entry in payPeriods.entries)
                      ChoiceChip(
                        label: Text(entry.key == 'one_off' ? 'One-off' : 'Per ${entry.value}'),
                        selected: _payPeriod == entry.key,
                        onSelected: (on) => setState(() => _payPeriod = on ? entry.key : null),
                      ),
                  ],
                ),
                const SizedBox(height: 20),
                _Field(label: 'Hours', controller: _availability, hint: 'Mon–Sat, 8am to 6pm', maxLength: 200),
              ],
          },
          FormError(_error),
          const SizedBox(height: 16),
          BusyButton(
            label: _editing ? 'Save' : 'Post',
            busy: _busy,
            onPressed: _category == null || _title.text.trim().isEmpty ? null : _submit,
          ),
        ],
      ),
    );
  }
}

class _Field extends StatelessWidget {
  const _Field({
    required this.label,
    required this.controller,
    required this.hint,
    required this.maxLength,
    this.lines = 1,
  });

  final String label;
  final TextEditingController controller;
  final String hint;
  final int maxLength;
  final int lines;

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 12),
      child: TextField(
        controller: controller,
        minLines: lines,
        maxLines: lines == 1 ? 1 : lines + 3,
        maxLength: maxLength,
        textCapitalization: TextCapitalization.sentences,
        decoration: InputDecoration(labelText: label, hintText: hint, alignLabelWithHint: lines > 1),
      ),
    );
  }
}

class _NumberField extends StatelessWidget {
  const _NumberField({required this.controller, required this.hint});

  final TextEditingController controller;
  final String hint;

  @override
  Widget build(BuildContext context) {
    return TextField(
      controller: controller,
      keyboardType: TextInputType.number,
      inputFormatters: [FilteringTextInputFormatter.digitsOnly, LengthLimitingTextInputFormatter(9)],
      decoration: InputDecoration(hintText: hint, prefixText: 'KES '),
    );
  }
}
