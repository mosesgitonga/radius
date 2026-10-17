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
import '../feed_controller.dart';
import '../feed_models.dart';

const _maxPhotos = 6;
const _maxLength = 2000;

/// Writing a post to the home area: a category, words, and up to six photos.
class ComposeScreen extends ConsumerStatefulWidget {
  const ComposeScreen({super.key});

  @override
  ConsumerState<ComposeScreen> createState() => _ComposeScreenState();
}

class _ComposeScreenState extends ConsumerState<ComposeScreen> {
  final _body = TextEditingController();
  String _category = 'general';
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
    try {
      final mediaIds = <String>[];
      for (var i = 0; i < _photos.length; i++) {
        setState(() => _progress = 'Uploading photo ${i + 1} of ${_photos.length}…');
        mediaIds.add(await uploader.uploadPhoto(_photos[i], purpose: 'post'));
      }
      setState(() => _progress = 'Posting…');
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
    final area = auth is AuthSignedIn ? (auth.me.areaLabel?.label ?? 'your area') : 'your area';

    return Scaffold(
      appBar: AppBar(
        leading: IconButton(
          onPressed: _busy ? null : () => context.pop(),
          tooltip: 'Cancel',
          icon: const Icon(Icons.close_rounded),
        ),
        title: Text('Post to $area'),
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
              child: const Text('Post'),
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
                      color: _category == entry.key ? Palette.white : Palette.ink,
                    ),
                    showCheckmark: false,
                    side: BorderSide(color: _category == entry.key ? Palette.primary : Palette.lineStrong),
                    backgroundColor: Palette.white,
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
            if (_photos.isNotEmpty)
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
                          icon: const Icon(Icons.close_rounded, color: Palette.white),
                        ),
                      ),
                    ],
                  ),
                ),
              ),
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
            const NotePanel(
              icon: Icons.location_on_outlined,
              text: 'Posted to your home area. Neighbours see the area and a rough '
                  'distance, never where you are. Photo location data is removed.',
            ),
            if (_progress != null) ...[
              const SizedBox(height: 16),
              Row(
                children: [
                  const SizedBox.square(
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
