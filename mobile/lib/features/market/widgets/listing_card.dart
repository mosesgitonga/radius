import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import '../../../core/theme.dart';
import '../listing_models.dart';

/// A listing's first photo, sized for where it shows, or a quiet placeholder.
class ListingPhoto extends StatelessWidget {
  const ListingPhoto({super.key, required this.listing, required this.width, this.fit = BoxFit.cover});

  final Listing listing;

  /// The width it is drawn at, in logical pixels.
  final double width;
  final BoxFit fit;

  @override
  Widget build(BuildContext context) {
    if (listing.images.isEmpty) {
      return ColoredBox(
        color: Palette.sand,
        child: Center(child: Icon(Icons.image_outlined, color: Palette.muted)),
      );
    }
    final pixels = (width * MediaQuery.devicePixelRatioOf(context)).round();
    return CachedNetworkImage(
      imageUrl: listing.images.first.urlFor(pixels),
      fit: fit,
      placeholder: (_, _) => ColoredBox(color: Palette.sand),
      errorWidget: (_, _, _) => ColoredBox(
        color: Palette.sand,
        child: Center(child: Icon(Icons.broken_image_outlined, color: Palette.muted)),
      ),
    );
  }
}

/// One listing in the grid: photo, price, title, where.
class ListingCard extends StatelessWidget {
  const ListingCard({super.key, required this.listing, required this.onTap});

  final Listing listing;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final badge = listing.isSold
        ? 'Sold'
        : listing.isExpired
            ? 'Expired'
            : listing.isReserved
                ? 'Reserved'
                : listing.isHeld
                    ? 'Held'
                    : null;
    return Material(
      color: Palette.surface,
      borderRadius: BorderRadius.circular(14),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onTap,
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            // The photo takes the room the text leaves, so larger text never
            // overflows the card; it only shrinks the photo a little.
            Expanded(
              child: Stack(
                fit: StackFit.expand,
                children: [
                  LayoutBuilder(builder: (context, c) => ListingPhoto(listing: listing, width: c.maxWidth)),
                  if (badge != null)
                    Positioned(
                      left: 8,
                      top: 8,
                      child: Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(
                          color: Palette.ink.withValues(alpha: 0.8),
                          borderRadius: BorderRadius.circular(10),
                        ),
                        child: Text(badge, style: const TextStyle(color: Palette.onPrimary, fontSize: 12)),
                      ),
                    ),
                ],
              ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(10, 8, 10, 10),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(listing.price, style: text.titleMedium?.copyWith(fontWeight: FontWeight.w700)),
                  const SizedBox(height: 2),
                  Text(listing.title, maxLines: 1, overflow: TextOverflow.ellipsis, style: text.bodyMedium),
                  const SizedBox(height: 2),
                  Text(listing.where,
                      maxLines: 1, overflow: TextOverflow.ellipsis, style: text.bodySmall?.copyWith(fontSize: 12.5)),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }
}
