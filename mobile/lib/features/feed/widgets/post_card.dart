import 'package:cached_network_image/cached_network_image.dart';
import 'package:flutter/material.dart';

import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../feed_models.dart';

/// A round avatar with the author's initials.
class InitialsAvatar extends StatelessWidget {
  const InitialsAvatar({super.key, required this.author, this.radius = 20});

  final Author author;
  final double radius;

  static const _tones = [
    Color(0xFFFBE7B5),
    Color(0xFFD6E4F7),
    Color(0xFFE8DDF5),
    Color(0xFFD7EFEA),
    Color(0xFFFDE2D5),
  ];

  @override
  Widget build(BuildContext context) {
    final tone = _tones[author.id.codeUnits.fold<int>(0, (a, b) => a + b) % _tones.length];
    return CircleAvatar(
      radius: radius,
      backgroundColor: tone,
      child: Text(
        author.initials,
        style: Theme.of(context).textTheme.titleSmall?.copyWith(fontSize: radius * 0.7),
      ),
    );
  }
}

/// The line under an author's name: area · distance band · time.
String postMeta(Post post) => [
      post.areaLabel.label,
      ?distanceLabel(post.distanceBucket),
      timeAgo(post.createdAt),
    ].join(' · ');

class CategoryTag extends StatelessWidget {
  const CategoryTag(this.category, {super.key});

  final String category;

  @override
  Widget build(BuildContext context) {
    final (bg, ink) = switch (category) {
      'alert' => (const Color(0xFFFDE2D5), const Color(0xFF8A2A06)),
      'lost_and_found' => (const Color(0xFFDCE8F7), const Color(0xFF163E6C)),
      'question' => (Palette.trustTint, Palette.trustInk),
      'recommendation' => (const Color(0xFFFBE7B5), const Color(0xFF5C3D00)),
      _ => (Palette.sand, Palette.inkSoft),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(12)),
      child: Text(
        postCategories[category] ?? category,
        style: Theme.of(context).textTheme.labelSmall?.copyWith(color: ink, fontWeight: FontWeight.w600),
      ),
    );
  }
}

/// A post, as a card in the feed or at the top of its own page.
class PostCard extends StatelessWidget {
  const PostCard({
    super.key,
    required this.post,
    required this.onHelpful,
    this.onOpen,
    this.onMore,
    this.mine = false,
  });

  final Post post;
  final VoidCallback onHelpful;
  final VoidCallback? onOpen;
  final VoidCallback? onMore;
  final bool mine;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final helpful = post.myReaction != null;
    return Material(
      color: Palette.white,
      borderRadius: BorderRadius.circular(16),
      clipBehavior: Clip.antiAlias,
      child: InkWell(
        onTap: onOpen,
        child: Padding(
          padding: const EdgeInsets.fromLTRB(14, 14, 6, 6),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  InitialsAvatar(author: post.author),
                  const SizedBox(width: 10),
                  Expanded(
                    child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Wrap(
                          spacing: 6,
                          crossAxisAlignment: WrapCrossAlignment.center,
                          children: [
                            Text(post.author.displayName, style: text.titleSmall),
                            if (post.author.isVerifiedNeighbour) const NeighbourBadge(),
                          ],
                        ),
                        Text(postMeta(post), style: text.bodySmall?.copyWith(fontSize: 12.5)),
                      ],
                    ),
                  ),
                  CategoryTag(post.category),
                  if (onMore != null)
                    IconButton(
                      onPressed: onMore,
                      tooltip: 'More',
                      icon: const Icon(Icons.more_vert_rounded, color: Palette.muted),
                    )
                  else
                    const SizedBox(width: 8),
                ],
              ),
              if (mine && post.isHeld)
                Container(
                  margin: const EdgeInsets.only(top: 10, right: 8),
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(
                    color: Palette.glowTint,
                    borderRadius: BorderRadius.circular(10),
                  ),
                  child: Text(
                    'Waiting for a moderator. Only you can see this post for now.',
                    style: text.bodySmall?.copyWith(color: Palette.primaryDark),
                  ),
                ),
              Padding(
                padding: const EdgeInsets.fromLTRB(0, 10, 8, 0),
                child: Text(post.body, style: text.bodyMedium),
              ),
              if (post.images.isNotEmpty)
                Padding(
                  padding: const EdgeInsets.fromLTRB(0, 10, 8, 0),
                  child: _Photos(images: post.images),
                ),
              Wrap(
                children: [
                  TextButton.icon(
                    onPressed: onHelpful,
                    style: TextButton.styleFrom(
                      foregroundColor: helpful ? Palette.primary : Palette.muted,
                      padding: const EdgeInsets.symmetric(horizontal: 6),
                    ),
                    icon: Icon(helpful ? Icons.thumb_up_alt_rounded : Icons.thumb_up_alt_outlined, size: 18),
                    label: Text('${post.reactionCount} helpful'),
                  ),
                  TextButton.icon(
                    onPressed: onOpen,
                    style: TextButton.styleFrom(
                      foregroundColor: Palette.muted,
                      padding: const EdgeInsets.symmetric(horizontal: 6),
                    ),
                    icon: const Icon(Icons.chat_bubble_outline_rounded, size: 18),
                    label: Text(post.commentCount == 1 ? '1 comment' : '${post.commentCount} comments'),
                  ),
                ],
              ),
            ],
          ),
        ),
      ),
    );
  }
}

class _Photos extends StatelessWidget {
  const _Photos({required this.images});

  final List<PostImage> images;

  @override
  Widget build(BuildContext context) {
    final width = (MediaQuery.sizeOf(context).width * MediaQuery.devicePixelRatioOf(context)).round();
    if (images.length == 1) {
      return ClipRRect(
        borderRadius: BorderRadius.circular(12),
        child: AspectRatio(aspectRatio: 4 / 3, child: _photo(images.first, width)),
      );
    }
    return SizedBox(
      height: 160,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        itemCount: images.length,
        separatorBuilder: (_, _) => const SizedBox(width: 8),
        itemBuilder: (_, index) => ClipRRect(
          borderRadius: BorderRadius.circular(12),
          child: SizedBox(width: 200, child: _photo(images[index], width ~/ 2)),
        ),
      ),
    );
  }

  Widget _photo(PostImage image, int width) => CachedNetworkImage(
        imageUrl: image.urlFor(width),
        fit: BoxFit.cover,
        placeholder: (_, _) => const ColoredBox(color: Palette.sand),
        errorWidget: (_, _, _) => const ColoredBox(
          color: Palette.sand,
          child: Center(child: Icon(Icons.broken_image_outlined, color: Palette.muted)),
        ),
      );
}
