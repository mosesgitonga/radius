import 'dart:typed_data';

import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../feed/feed_models.dart';
import '../group_models.dart';
import '../group_repository.dart';
import '../widgets/group_widgets.dart';

/// Starting a group, or with [editing] changing one. Closes with the group as the
/// server has it.
class GroupEditScreen extends ConsumerStatefulWidget {
  const GroupEditScreen({super.key, this.editing});

  final Group? editing;

  @override
  ConsumerState<GroupEditScreen> createState() => _GroupEditScreenState();
}

class _GroupEditScreenState extends ConsumerState<GroupEditScreen> {
  late final _name = TextEditingController(text: widget.editing?.name);
  late final _description = TextEditingController(text: widget.editing?.description);
  late final _rules = TextEditingController(text: widget.editing?.rules);
  late String _category = widget.editing?.category ?? 'estate';
  late bool _isPublic = widget.editing?.isPublic ?? true;
  late bool _needsApproval = widget.editing?.needsApproval ?? false;
  late bool _approvesPosts = widget.editing?.approvesPosts ?? false;
  Uint8List? _cover;
  bool _busy = false;
  String? _error;

  bool get _editing => widget.editing != null;

  @override
  void dispose() {
    _name.dispose();
    _description.dispose();
    _rules.dispose();
    super.dispose();
  }

  Future<void> _pickCover() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      maxWidth: 2048,
      maxHeight: 2048,
      imageQuality: 85,
    );
    if (picked == null) return;
    final bytes = await picked.readAsBytes();
    if (mounted) setState(() => _cover = bytes);
  }

  Future<void> _save() async {
    final name = _name.text.trim();
    if (name.isEmpty) {
      setState(() => _error = 'Give the group a name.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
      final cover = _cover;
      final coverId =
          cover == null ? null : await ref.read(mediaUploaderProvider).uploadPhoto(cover, purpose: 'group');
      final rules = _rules.text.trim();
      final fields = GroupRepository.fields(
        name: name,
        description: _description.text.trim(),
        category: _category,
        isPublic: _isPublic,
        needsApproval: _isPublic && _needsApproval,
        approvesPosts: _approvesPosts,
        rules: rules.isEmpty ? null : rules,
        coverMediaId: coverId,
      );
      final repo = ref.read(groupRepositoryProvider);
      final group = _editing ? await repo.update(widget.editing!.id, fields) : await repo.create(fields);
      if (mounted) context.pop(group);
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final existingCover = widget.editing == null ? null : sizeFor(widget.editing!.cover, 1080);

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          onPressed: _busy ? null : () => context.pop(),
          tooltip: 'Cancel',
          icon: const Icon(Icons.close_rounded),
        ),
        title: Text(_editing ? 'Edit group' : 'New group'),
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
          children: [
            GestureDetector(
              onTap: _busy ? null : _pickCover,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(16),
                child: AspectRatio(
                  aspectRatio: 16 / 7,
                  child: _cover != null
                      ? Image.memory(_cover!, fit: BoxFit.cover)
                      : existingCover != null
                          ? CachedNetworkImage(imageUrl: existingCover, fit: BoxFit.cover)
                          : Container(
                              color: Palette.sand,
                              alignment: Alignment.center,
                              child: Column(
                                mainAxisSize: MainAxisSize.min,
                                children: [
                                  Icon(Icons.add_photo_alternate_outlined, color: Palette.muted),
                                  const SizedBox(height: 4),
                                  Text('Add a cover photo (optional)', style: text.bodySmall),
                                ],
                              ),
                            ),
                ),
              ),
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _name,
              enabled: !_busy,
              maxLength: 200,
              textCapitalization: TextCapitalization.words,
              decoration: const InputDecoration(labelText: 'Name', hintText: 'Kamakwa Estate Residents', counterText: ''),
            ),
            const SizedBox(height: 12),
            Text('Category', style: text.labelMedium?.copyWith(color: Palette.muted)),
            const SizedBox(height: 6),
            Wrap(
              runSpacing: 8,
              children: [
                for (final entry in groupCategories.entries)
                  GroupCategoryChip(
                    label: entry.value,
                    selected: _category == entry.key,
                    onTap: _busy ? null : () => setState(() => _category = entry.key),
                  ),
              ],
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _description,
              enabled: !_busy,
              minLines: 2,
              maxLines: 6,
              maxLength: 2000,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(labelText: 'What is it for?', counterText: ''),
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _rules,
              enabled: !_busy,
              minLines: 2,
              maxLines: 8,
              maxLength: 4000,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(labelText: 'Rules (optional)', counterText: ''),
            ),
            const SizedBox(height: 8),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Public'),
              subtitle: Text(_isPublic
                  ? 'Neighbours nearby can find it and read it.'
                  : 'Hidden. People join only by invitation or an invite link.'),
              value: _isPublic,
              onChanged: _busy ? null : (value) => setState(() => _isPublic = value),
            ),
            if (_isPublic)
              SwitchListTile(
                contentPadding: EdgeInsets.zero,
                title: const Text('Approve new members'),
                subtitle: const Text('People ask to join, and an admin lets them in.'),
                value: _needsApproval,
                onChanged: _busy ? null : (value) => setState(() => _needsApproval = value),
              ),
            SwitchListTile(
              contentPadding: EdgeInsets.zero,
              title: const Text('Approve posts'),
              subtitle: const Text("Members' posts wait for a moderator before others see them."),
              value: _approvesPosts,
              onChanged: _busy ? null : (value) => setState(() => _approvesPosts = value),
            ),
            if (_isPublic && !(widget.editing?.isPublic ?? false)) ...[
              const SizedBox(height: 8),
              const NotePanel(
                icon: Icons.location_on_outlined,
                text: 'A public group is placed at your home area, and found by neighbours around it. '
                    'People see the area name, never where you live.',
              ),
            ],
            FormError(_error),
            const SizedBox(height: 16),
            BusyButton(label: _editing ? 'Save' : 'Create group', busy: _busy, onPressed: _save),
          ],
        ),
      ),
    );
  }
}
