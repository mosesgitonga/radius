import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../auth/auth_controller.dart';
import '../../groups/group_models.dart' show groupPostPostedMessage;
import '../feed_controller.dart';
import '../feed_models.dart';

const _maxPhotos = 6;
const _maxLength = 2000;

/// Writing a post: a category, words, and up to six photos. To the home area, or
/// with [groupId] to that group, in which case the new post is handed back to
/// the group screen when this closes. With [editing], it changes one of your
/// posts instead: text and category only (decision 2.22).
class ComposeScreen extends ConsumerStatefulWidget {
  const ComposeScreen({super.key, this.groupId, this.groupName, this.editing});

  final String? groupId;
  final String? groupName;
  final Post? editing;

  @override
  ConsumerState<ComposeScreen> createState() => _ComposeScreenState();
}

class _ComposeScreenState extends ConsumerState<ComposeScreen> {
  late final _body = TextEditingController(text: widget.editing?.body);
  late String _category = widget.editing?.category ?? 'general';
  final List<Uint8List> _photos = [];
  bool _busy = false;
  String? _progress;
  String? _error;

  @override
  void dispose() {
    _body.dispose();
    super.dispose();
  }

  Future<void> _addPhotos() async {
    final room = _maxPhotos - _photos.length;
    if (room <= 0) return;
    // Resized on the phone first: no point paying to upload pixels the server
    // would throw away.
    final picked = await ImagePicker().pickMultiImage(
      maxWidth: 2048,
      maxHeight: 2048,
      imageQuality: 85,
      limit: room,
    );
    for (final file in picked.take(room)) {
      final bytes = await file.readAsBytes();
      if (mounted) setState(() => _photos.add(bytes));
    }
  }

  Future<void> _post() async {
    final body = _body.text.trim();
    if (body.isEmpty) {
      setState(() => _error = 'Write something first.');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    final repo = ref.read(feedRepositoryProvider);
    final uploader = ref.read(mediaUploaderProvider);
    final editing = widget.editing;
    try {
      if (editing != null) {
        final post = await repo.editPost(editing.id, body: body, category: _category);
        if (!mounted) return;
        context.pop(post);
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(
          content: Text(post.awaitsApproval && !editing.awaitsApproval
              ? 'Saved. It goes back to a moderator for approval.'
              : post.isHeld && !editing.isHeld
                  ? 'Saved. It will show again once a moderator has looked at it.'
                  : 'Post updated.'),
        ));
        return;
      }
      final mediaIds = <String>[];
      for (var i = 0; i < _photos.length; i++) {
        setState(() => _progress = 'Uploading photo ${i + 1} of ${_photos.length}…');
        mediaIds.add(await uploader.uploadPhoto(_photos[i], purpose: 'post'));
      }
      setState(() => _progress = 'Posting…');
      final groupId = widget.groupId;
      if (groupId != null) {
        final post = await ref
            .read(groupRepositoryProvider)
            .createPost(groupId, body: body, category: _category, mediaIds: mediaIds);
        if (!mounted) return;
        context.pop(post);
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(groupPostPostedMessage(post))));
        return;
      }
      final (post, reach) = await repo.createPost(body: body, category: _category, mediaIds: mediaIds);
      ref.read(feedControllerProvider.notifier).prepend(post);
      if (!mounted) return;
      context.pop();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(
        content: Text(post.isHeld
            ? 'Posted. It will appear once a moderator has looked at it.'
            : 'Posted. Visible to about $reach neighbours.'),
      ));
    } on ApiException catch (error) {
      setState(() => _error = error.message);
    } finally {
      if (mounted) {
        setState(() {
          _busy = false;
          _progress = null;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final label = auth is AuthSignedIn ? auth.me.areaLabel : null;
    final area = label == null || label.precision == 'generic' ? 'your area' : label.label;

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          onPressed: _busy ? null : () => context.pop(),
          tooltip: 'Cancel',
          icon: const Icon(Icons.close_rounded),
        ),
        title: Text(
          widget.editing != null ? 'Edit post' : 'Post to ${widget.groupName ?? area}',
          overflow: TextOverflow.ellipsis,
        ),
        actions: [
          Padding(
            padding: const EdgeInsets.only(right: 8),
            child: FilledButton(
              onPressed: _busy ? null : _post,
              style: FilledButton.styleFrom(
                minimumSize: const Size(72, 40),
                padding: const EdgeInsets.symmetric(horizontal: 18),
                shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(20)),
              ),
              child: Text(widget.editing != null ? 'Save' : 'Post'),
            ),
          ),
        ],
      ),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            Text('What kind of post?', style: text.labelMedium?.copyWith(color: Palette.muted)),
            const SizedBox(height: 8),
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                for (final entry in postCategories.entries)
                  ChoiceChip(
                    label: Text(entry.value),
                    selected: _category == entry.key,
                    onSelected: _busy ? null : (_) => setState(() => _category = entry.key),
                    selectedColor: Palette.primary,
                    labelStyle: text.labelMedium?.copyWith(
                      color: _category == entry.key ? Palette.onPrimary : Palette.ink,
                    ),
                    showCheckmark: false,
                    side: BorderSide(color: _category == entry.key ? Palette.primary : Palette.lineStrong),
                    backgroundColor: Palette.surface,
                  ),
              ],
            ),
            const SizedBox(height: 16),
            TextField(
              controller: _body,
              enabled: !_busy,
              autofocus: true,
              minLines: 5,
              maxLines: 12,
              maxLength: _maxLength,
              textCapitalization: TextCapitalization.sentences,
              decoration: const InputDecoration(
                hintText: 'Ask a question, share a tip, report something lost or found…',
              ),
            ),
            if (widget.editing != null)
              NotePanel(
                icon: Icons.history_rounded,
                text: widget.editing!.images.isEmpty
                    ? 'The post will show it was edited.'
                    : "Photos can't be changed: to change them, delete the post and post again. "
                        'The post will show it was edited.',
              ),
            if (widget.editing == null && _photos.isNotEmpty)
              SizedBox(
                height: 96,
                child: ListView.separated(
                  scrollDirection: Axis.horizontal,
                  itemCount: _photos.length,
                  separatorBuilder: (_, _) => const SizedBox(width: 8),
                  itemBuilder: (_, index) => Stack(
                    children: [
                      ClipRRect(
                        borderRadius: BorderRadius.circular(10),
                        child: Image.memory(_photos[index], width: 96, height: 96, fit: BoxFit.cover),
                      ),
                      Positioned(
                        top: 2,
                        right: 2,
                        child: IconButton.filled(
                          onPressed: _busy ? null : () => setState(() => _photos.removeAt(index)),
                          tooltip: 'Remove photo',
                          iconSize: 16,
                          visualDensity: VisualDensity.compact,
                          style: IconButton.styleFrom(backgroundColor: Palette.ink),
                          icon: const Icon(Icons.close_rounded, color: Palette.onPrimary),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
            if (widget.editing == null) ...[
            const SizedBox(height: 12),
            Align(
              alignment: Alignment.centerLeft,
              child: OutlinedButton.icon(
                onPressed: _busy || _photos.length >= _maxPhotos ? null : _addPhotos,
                style: OutlinedButton.styleFrom(minimumSize: const Size(0, 44)),
                icon: const Icon(Icons.add_photo_alternate_outlined),
                label: Text(_photos.isEmpty ? 'Add photos' : 'Add photos (${_photos.length}/$_maxPhotos)'),
              ),
            ),
            const SizedBox(height: 16),
            ],
            if (widget.editing != null)
              const SizedBox.shrink()
            else if (widget.groupId != null)
              const NotePanel(
                icon: Icons.groups_outlined,
                text: "Posted in the group only; it doesn't reach the neighbourhood feed. "
                    'Photo location data is removed.',
              )
            else
              const NotePanel(
                icon: Icons.location_on_outlined,
                text: 'Posted to your home area. Neighbours see the area and a rough '
                    'distance, never where you are. Photo location data is removed.',
              ),
            if (_progress != null) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  SizedBox.square(
                    dimension: 18,
                    child: CircularProgressIndicator(strokeWidth: 2, color: Palette.primary),
                  ),
                  const SizedBox(width: 10),
                  Text(_progress!, style: text.bodyMedium),
                ],
              ),
            ],
            FormError(_error),
          ],
        ),
      ),
    );
  }
}
