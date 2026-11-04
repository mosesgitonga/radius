import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:image_picker/image_picker.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import 'stories_controller.dart';
import 'story_models.dart';

/// Posting a story: a photo, or a line of text on a coloured card. It goes to my
/// home area, like a post, and is gone in a day.
class StoryComposeScreen extends ConsumerStatefulWidget {
  const StoryComposeScreen({super.key});

  static const maxText = 280;

  @override
  ConsumerState<StoryComposeScreen> createState() => _StoryComposeScreenState();
}

class _StoryComposeScreenState extends ConsumerState<StoryComposeScreen> {
  final _text = TextEditingController();
  Uint8List? _photo;
  bool _busy = false;
  String? _error;
  String? _progress;

  @override
  void initState() {
    super.initState();
    _text.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _text.dispose();
    super.dispose();
  }

  Future<void> _pickPhoto() async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      maxWidth: 2048,
      maxHeight: 2048,
      imageQuality: 85,
    );
    if (picked == null) return;
    final bytes = await picked.readAsBytes();
    if (mounted) setState(() => _photo = bytes);
  }

  bool get _ready => _photo != null || _text.text.trim().isNotEmpty;

  Future<void> _post() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final stories = ref.read(storyRepositoryProvider);
    try {
      final Story story;
      final photo = _photo;
      if (photo != null) {
        setState(() => _progress = 'Uploading photo…');
        final mediaId = await ref.read(mediaUploaderProvider).uploadPhoto(photo, purpose: 'story');
        story = await stories.postImage(mediaId);
      } else {
        story = await stories.postText(_text.text);
      }
      ref.read(storiesControllerProvider.notifier).added(story);
      if (mounted) Navigator.of(context).pop();
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
    final photo = _photo;
    return Scaffold(
      appBar: AppBar(title: const Text('New story')),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.fromLTRB(16, 8, 16, 24),
          children: [
            AspectRatio(
              aspectRatio: 9 / 12,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(16),
                child: photo != null
                    ? Stack(
                        fit: StackFit.expand,
                        children: [
                          Image.memory(photo, fit: BoxFit.cover),
                          Align(
                            alignment: Alignment.topRight,
                            child: IconButton(
                              tooltip: 'Remove photo',
                              onPressed: _busy ? null : () => setState(() => _photo = null),
                              icon: const Icon(Icons.close_rounded, color: Palette.white),
                            ),
                          ),
                        ],
                      )
                    : Container(
                        color: Color(storyColours.first),
                        alignment: Alignment.center,
                        padding: const EdgeInsets.all(24),
                        child: TextField(
                          controller: _text,
                          enabled: !_busy,
                          maxLength: StoryComposeScreen.maxText,
                          maxLines: null,
                          textAlign: TextAlign.center,
                          style: text.headlineSmall?.copyWith(color: Palette.white, height: 1.3),
                          cursorColor: Palette.white,
                          decoration: InputDecoration(
                            border: InputBorder.none,
                            filled: false,
                            hintText: "What's happening?",
                            hintStyle: text.headlineSmall?.copyWith(color: Colors.white60),
                            counterStyle: const TextStyle(color: Colors.white70),
                          ),
                        ),
                      ),
              ),
            ),
            const SizedBox(height: 12),
            if (photo == null)
              OutlinedButton.icon(
                onPressed: _busy ? null : _pickPhoto,
                icon: const Icon(Icons.photo_outlined),
                label: const Text('Use a photo instead'),
              ),
            const SizedBox(height: 8),
            Text(
              'Neighbours around your home see it for 24 hours. Photos have their location data removed.',
              style: text.bodySmall,
            ),
            if (_progress != null) ...[const SizedBox(height: 8), Text(_progress!, style: text.bodySmall)],
            FormError(_error),
            const SizedBox(height: 16),
            BusyButton(label: 'Share story', busy: _busy, onPressed: _ready ? _post : null),
          ],
        ),
      ),
    );
  }
}
