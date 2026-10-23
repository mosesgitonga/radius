import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:image_picker/image_picker.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../feed/feed_models.dart';
import '../listing_models.dart';

const _maxPhotos = 8;

/// A photo on the form: one already on the listing, or one just picked.
sealed class _Photo {}

class _Uploaded extends _Photo {
  _Uploaded(this.image);
  final PostImage image;
}

class _Picked extends _Photo {
  _Picked(this.bytes);
  final Uint8List bytes;
}

/// Selling something, or editing a listing of mine.
class SellScreen extends ConsumerStatefulWidget {
  const SellScreen({super.key, this.editing});

  final Listing? editing;

  @override
  ConsumerState<SellScreen> createState() => _SellScreenState();
}

class _SellScreenState extends ConsumerState<SellScreen> {
  final _title = TextEditingController();
  final _description = TextEditingController();
  final _price = TextEditingController();
  final List<_Photo> _photos = [];
  String? _category;
  String _condition = 'good';
  String _pricing = 'fixed';
  bool _busy = false;
  String? _progress;
  String? _error;

  bool get _editing => widget.editing != null;

  @override
  void initState() {
    super.initState();
    final listing = widget.editing;
    if (listing != null) {
      _title.text = listing.title;
      _description.text = listing.description;
      _category = listing.category;
      _condition = listing.condition;
      _pricing = listing.pricing;
      if (listing.pricing != 'free') _price.text = '${listing.priceKes}';
      _photos.addAll(listing.images.map(_Uploaded.new));
    }
    _title.addListener(() => setState(() {}));
    _price.addListener(() => setState(() {}));
  }

  @override
  void dispose() {
    _title.dispose();
    _description.dispose();
    _price.dispose();
    super.dispose();
  }

  Future<void> _addPhotos() async {
    final room = _maxPhotos - _photos.length;
    if (room <= 0) return;
    // Resized on the phone first: no point uploading pixels the server discards.
    final picked = await ImagePicker().pickMultiImage(maxWidth: 2048, maxHeight: 2048, imageQuality: 85, limit: room);
    for (final file in picked.take(room)) {
      final bytes = await file.readAsBytes();
      if (mounted) setState(() => _photos.add(_Picked(bytes)));
    }
  }

  bool get _ready =>
      _category != null && _title.text.trim().isNotEmpty && (_pricing == 'free' || int.tryParse(_price.text) != null);

  Future<void> _submit() async {
    setState(() {
      _busy = true;
      _error = null;
    });
    final uploader = ref.read(mediaUploaderProvider);
    final repo = ref.read(listingRepositoryProvider);
    try {
      final mediaIds = <String>[];
      final toUpload = _photos.whereType<_Picked>().length;
      var uploaded = 0;
      for (final photo in _photos) {
        switch (photo) {
          case _Uploaded(:final image):
            mediaIds.add(image.mediaId);
          case _Picked(:final bytes):
            uploaded++;
            setState(() => _progress = 'Uploading photo $uploaded of $toUpload…');
            mediaIds.add(await uploader.uploadPhoto(bytes, purpose: 'listing'));
        }
      }
      setState(() => _progress = _editing ? 'Saving…' : 'Listing…');
      final fields = {
        'title': _title.text.trim(),
        'description': _description.text.trim(),
        'category': _category,
        'condition': _condition,
        'pricing': _pricing,
        'price_kes': _pricing == 'free' ? 0 : int.parse(_price.text),
        'media_ids': mediaIds,
      };
      if (_editing) {
        final updated = await repo.update(widget.editing!.id, fields);
        if (mounted) context.pop(updated);
        return;
      }
      final listing = await repo.create(fields);
      if (!mounted) return;
      // The listing page says how many neighbours can see it.
      context.go(Routes.listing(listing.id));
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _busy = false;
          _progress = null;
          _error = _explain(error);
        });
      }
    }
  }

  /// The server's words, plus the term that tripped the prohibited-items check,
  /// so an honest seller knows what to rephrase.
  static String _explain(ApiException error) {
    final term = error.details['term'];
    if (error.details['reason'] == 'prohibited_item' && term is String) {
      return '${error.message}: it mentions "$term".';
    }
    return error.message;
  }

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Scaffold(
      appBar: AppBar(title: Text(_editing ? 'Edit listing' : 'Sell something')),
      body: ListView(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 32),
        children: [
          Text('Photos', style: text.titleSmall),
          const SizedBox(height: 4),
          Text('Up to $_maxPhotos. The first is the one people see in the Market.', style: text.bodySmall),
          const SizedBox(height: 8),
          SizedBox(
            height: 96,
            child: ListView(
              scrollDirection: Axis.horizontal,
              children: [
                for (var i = 0; i < _photos.length; i++)
                  Padding(
                    padding: const EdgeInsets.only(right: 8),
                    child: Stack(
                      children: [
                        ClipRRect(
                          borderRadius: BorderRadius.circular(10),
                          child: SizedBox.square(
                            dimension: 96,
                            child: switch (_photos[i]) {
                              _Uploaded(:final image) => CachedNetworkImage(
                                  imageUrl: image.urlFor(300),
                                  fit: BoxFit.cover,
                                ),
                              _Picked(:final bytes) => Image.memory(bytes, fit: BoxFit.cover),
                            },
                          ),
                        ),
                        Positioned(
                          right: 2,
                          top: 2,
                          child: IconButton.filled(
                            tooltip: 'Remove photo',
                            visualDensity: VisualDensity.compact,
                            style: IconButton.styleFrom(backgroundColor: Palette.ink.withValues(alpha: 0.7)),
                            onPressed: _busy ? null : () => setState(() => _photos.removeAt(i)),
                            icon: const Icon(Icons.close_rounded, size: 16, color: Palette.white),
                          ),
                        ),
                      ],
                    ),
                  ),
                if (_photos.length < _maxPhotos)
                  InkWell(
                    onTap: _busy ? null : _addPhotos,
                    borderRadius: BorderRadius.circular(10),
                    child: Container(
                      width: 96,
                      decoration: BoxDecoration(
                        borderRadius: BorderRadius.circular(10),
                        border: Border.all(color: Palette.lineStrong),
                        color: Palette.white,
                      ),
                      child: const Column(
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Icon(Icons.add_a_photo_outlined, color: Palette.inkSoft),
                          SizedBox(height: 4),
                          Text('Add', style: TextStyle(fontSize: 13)),
                        ],
                      ),
                    ),
                  ),
              ],
            ),
          ),
          const SizedBox(height: 20),
          TextField(
            controller: _title,
            maxLength: 120,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(labelText: 'What are you selling?', hintText: 'Three-seater sofa, grey'),
          ),
          const SizedBox(height: 8),
          TextField(
            controller: _description,
            minLines: 4,
            maxLines: 8,
            maxLength: 4000,
            textCapitalization: TextCapitalization.sentences,
            decoration: const InputDecoration(
              labelText: 'Details',
              hintText: 'Age, size, any faults, and when it can be collected',
              alignLabelWithHint: true,
            ),
          ),
          const SizedBox(height: 12),
          Text('Category', style: text.titleSmall),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final entry in listingCategories.entries)
                ChoiceChip(
                  label: Text(entry.value),
                  selected: _category == entry.key,
                  onSelected: (_) => setState(() => _category = entry.key),
                ),
            ],
          ),
          const SizedBox(height: 20),
          Text('Condition', style: text.titleSmall),
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 8,
            children: [
              for (final entry in listingConditions.entries)
                ChoiceChip(
                  label: Text(entry.value),
                  selected: _condition == entry.key,
                  onSelected: (_) => setState(() => _condition = entry.key),
                ),
            ],
          ),
          const SizedBox(height: 20),
          Text('Price', style: text.titleSmall),
          const SizedBox(height: 8),
          SegmentedButton<String>(
            showSelectedIcon: false,
            segments: [
              for (final entry in listingPricings.entries) ButtonSegment(value: entry.key, label: Text(entry.value)),
            ],
            selected: {_pricing},
            onSelectionChanged: (value) => setState(() => _pricing = value.single),
          ),
          if (_pricing != 'free') ...[
            const SizedBox(height: 12),
            TextField(
              controller: _price,
              keyboardType: TextInputType.number,
              inputFormatters: [FilteringTextInputFormatter.digitsOnly, LengthLimitingTextInputFormatter(9)],
              decoration: const InputDecoration(labelText: 'Price', prefixText: 'KES '),
            ),
          ],
          const SizedBox(height: 16),
          const NotePanel(
            icon: Icons.place_outlined,
            text: 'Neighbours see your area, not your address. Agree where to meet in the chat.',
          ),
          FormError(_error),
          if (_progress != null) ...[
            const SizedBox(height: 12),
            Text(_progress!, style: text.bodySmall),
          ],
          const SizedBox(height: 16),
          BusyButton(
            label: _editing ? 'Save' : 'List it',
            busy: _busy,
            onPressed: _ready ? _submit : null,
          ),
        ],
      ),
    );
  }
}
