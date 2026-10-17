import 'package:flutter/material.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';

const _reasons = <String, String>{
  'spam': 'Spam or advertising',
  'scam': 'A scam or fraud',
  'harassment': 'Harassment or bullying',
  'hate_speech': 'Hate speech',
  'violence': 'Violence or a threat',
  'sexual_content': 'Sexual content',
  'misinformation': 'False information',
  'other': 'Something else',
};

/// Asks why, then sends the report. Says thanks and nothing more: whether
/// anything was hidden is not the reporter's to know.
Future<void> showReportSheet(
  BuildContext context, {
  required String what,
  required Future<void> Function(String reason, String? details) send,
}) {
  return showModalBottomSheet<void>(
    context: context,
    isScrollControlled: true,
    backgroundColor: Palette.cream,
    shape: const RoundedRectangleBorder(borderRadius: BorderRadius.vertical(top: Radius.circular(20))),
    builder: (_) => _ReportSheet(what: what, send: send),
  );
}

class _ReportSheet extends StatefulWidget {
  const _ReportSheet({required this.what, required this.send});

  final String what;
  final Future<void> Function(String reason, String? details) send;

  @override
  State<_ReportSheet> createState() => _ReportSheetState();
}

class _ReportSheetState extends State<_ReportSheet> {
  String? _reason;
  final _details = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _details.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      await widget.send(_reason!, _details.text);
      if (!mounted) return;
      Navigator.of(context).pop();
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Thanks. A moderator will look at it.')),
      );
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Padding(
      padding: EdgeInsets.only(bottom: MediaQuery.viewInsetsOf(context).bottom),
      child: SafeArea(
        child: SingleChildScrollView(
          padding: const EdgeInsets.fromLTRB(20, 16, 20, 20),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: [
              Text('Report this ${widget.what}', style: text.titleLarge),
              const SizedBox(height: 6),
              Text('Reports are seen by moderators only. Nobody is told who reported.',
                  style: text.bodySmall),
              const SizedBox(height: 8),
              RadioGroup<String>(
                groupValue: _reason,
                onChanged: (value) => setState(() => _reason = value),
                child: Column(
                  children: [
                    for (final entry in _reasons.entries)
                      RadioListTile<String>(
                        value: entry.key,
                        title: Text(entry.value, style: text.bodyMedium),
                        contentPadding: EdgeInsets.zero,
                        activeColor: Palette.primary,
                      ),
                  ],
                ),
              ),
              TextField(
                controller: _details,
                maxLength: 1000,
                maxLines: 3,
                minLines: 1,
                decoration: const InputDecoration(hintText: 'Anything a moderator should know (optional)'),
              ),
              FormError(_error),
              const SizedBox(height: 8),
              BusyButton(label: 'Send report', busy: _busy, onPressed: _reason == null ? null : _submit),
            ],
          ),
        ),
      ),
    );
  }
}
