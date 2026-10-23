import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/theme.dart';
import '../../core/widgets.dart';
import '../auth/auth_controller.dart';
import '../feed/feed_models.dart';
import '../feed/widgets/post_card.dart';

/// Editing my profile: photo, cover, name and bio. Photos are saved as soon as
/// they are chosen; the name and bio with Save.
class EditProfileScreen extends ConsumerStatefulWidget {
  const EditProfileScreen({super.key});

  @override
  ConsumerState<EditProfileScreen> createState() => _EditProfileScreenState();
}

class _EditProfileScreenState extends ConsumerState<EditProfileScreen> {
  final _name = TextEditingController();
  final _bio = TextEditingController();
  bool _saving = false;
  String? _photoBusy;
  String? _error;

  @override
  void initState() {
    super.initState();
    final auth = ref.read(authControllerProvider);
    if (auth is AuthSignedIn) {
      _name.text = auth.me.displayName;
      _bio.text = auth.me.bio ?? '';
    }
    _bio.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _name.dispose();
    _bio.dispose();
    super.dispose();
  }

  void _toast(String message) {
    if (mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(message)));
  }

  /// Picks, uploads and sets a photo; [cover] chooses which.
  Future<void> _pick({required bool cover}) async {
    final picked = await ImagePicker().pickImage(
      source: ImageSource.gallery,
      maxWidth: cover ? 2048 : 1024,
      maxHeight: cover ? 2048 : 1024,
      imageQuality: 85,
    );
    if (picked == null) return;
    setState(() => _photoBusy = cover ? 'Uploading your cover…' : 'Uploading your photo…');
    try {
      final bytes = await picked.readAsBytes();
      final mediaId = await ref
          .read(mediaUploaderProvider)
          .uploadPhoto(bytes, purpose: cover ? 'cover' : 'avatar');
      final repo = ref.read(profileRepositoryProvider);
      await (cover ? repo.setCover(mediaId) : repo.setAvatar(mediaId));
      await ref.read(authControllerProvider.notifier).refreshMe();
    } on ApiException catch (error) {
      _toast(error.message);
    } finally {
      if (mounted) setState(() => _photoBusy = null);
    }
  }

  Future<void> _remove({required bool cover}) async {
    try {
      final repo = ref.read(profileRepositoryProvider);
      await (cover ? repo.setCover(null) : repo.setAvatar(null));
      await ref.read(authControllerProvider.notifier).refreshMe();
    } on ApiException catch (error) {
      _toast(error.message);
    }
  }

  Future<void> _save() async {
    if (_name.text.trim().length < 2) {
      setState(() => _error = 'A name needs at least 2 letters.');
      return;
    }
    setState(() {
      _saving = true;
      _error = null;
    });
    try {
      final me = await ref
          .read(profileRepositoryProvider)
          .updateDetails(displayName: _name.text, bio: _bio.text);
      ref.read(authControllerProvider.notifier).replaceMe(me);
      if (mounted) context.pop();
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _saving = false;
          _error = error.message;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final auth = ref.watch(authControllerProvider);
    final me = auth is AuthSignedIn ? auth.me : null;
    if (me == null) return const Scaffold();
    final width = MediaQuery.sizeOf(context).width;
    final coverUrl = sizeFor(me.cover, (width * MediaQuery.devicePixelRatioOf(context)).round());

    return Scaffold(
      appBar: AppBar(title: const Text('Edit profile')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text('Cover photo', style: text.titleSmall),
          const SizedBox(height: 8),
          ClipRRect(
            borderRadius: BorderRadius.circular(14),
            child: SizedBox(
              height: 130,
              child: coverUrl == null
                  ? const ColoredBox(
                      color: Palette.sand,
                      child: Center(child: Icon(Icons.landscape_outlined)),
                    )
                  : CachedNetworkImage(imageUrl: coverUrl, fit: BoxFit.cover),
            ),
          ),
          Row(
            children: [
              TextButton.icon(
                onPressed: _photoBusy != null ? null : () => _pick(cover: true),
                icon: const Icon(Icons.photo_outlined, size: 18),
                label: Text(me.cover.isEmpty ? 'Add a cover' : 'Change cover'),
              ),
              if (me.cover.isNotEmpty)
                TextButton(
                  onPressed: _photoBusy != null ? null : () => _remove(cover: true),
                  style: TextButton.styleFrom(foregroundColor: Palette.danger),
                  child: const Text('Remove'),
                ),
            ],
          ),
          const SizedBox(height: 12),
          Text('Profile photo', style: text.titleSmall),
          const SizedBox(height: 8),
          Row(
            children: [
              InitialsAvatar(
                author: Author(
                  id: me.id,
                  displayName: me.displayName,
                  isVerifiedNeighbour: me.isVerifiedNeighbour,
                  avatar: me.avatar,
                ),
                radius: 36,
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Wrap(
                  children: [
                    TextButton.icon(
                      onPressed: _photoBusy != null ? null : () => _pick(cover: false),
                      icon: const Icon(Icons.face_outlined, size: 18),
                      label: Text(me.avatar.isEmpty ? 'Add a photo' : 'Change photo'),
                    ),
                    if (me.avatar.isNotEmpty)
                      TextButton(
                        onPressed: _photoBusy != null ? null : () => _remove(cover: false),
                        style: TextButton.styleFrom(foregroundColor: Palette.danger),
                        child: const Text('Remove'),
                      ),
                  ],
                ),
              ),
            ],
          ),
          if (_photoBusy != null) ...[
            const SizedBox(height: 8),
            Row(
              children: [
                const SizedBox.square(
                  dimension: 16,
                  child: CircularProgressIndicator(strokeWidth: 2, color: Palette.primary),
                ),
                const SizedBox(width: 10),
                Text(_photoBusy!, style: text.bodySmall),
              ],
            ),
          ],
          const SizedBox(height: 8),
          Text('Photos are public to anyone who can see your profile.', style: text.bodySmall),
          const SizedBox(height: 20),
          TextField(
            controller: _name,
            maxLength: 50,
            textCapitalization: TextCapitalization.words,
            decoration: const InputDecoration(
              labelText: 'Name',
              helperText: 'How neighbours see you. A first name and an initial is enough.',
            ),
          ),
          const SizedBox(height: 12),
          TextField(
            controller: _bio,
            minLines: 2,
            maxLines: 4,
            maxLength: 160,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'Bio',
              hintText: "Plumber in Kamakwa, 10 years' experience. Weekdays from 7am.",
              alignLabelWithHint: true,
            ),
          ),
          const NotePanel(
            icon: Icons.shield_outlined,
            text:
                "Keep your phone number and address out of your bio. Share them in a chat when you're ready.",
          ),
          FormError(_error),
          const SizedBox(height: 16),
          BusyButton(label: 'Save', busy: _saving, onPressed: _save),
        ],
      ),
    );
  }
}
