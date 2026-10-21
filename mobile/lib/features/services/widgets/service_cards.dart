import 'package:flutter/material.dart';

import '../../../core/theme.dart';
import '../../../core/widgets.dart';
import '../../feed/feed_models.dart';
import '../../feed/widgets/post_card.dart';
import '../service_models.dart';

/// One post in the browse list, laid out for its kind.
class ServiceCard extends StatelessWidget {
  const ServiceCard({super.key, required this.post, required this.onOpen, this.onHelp});

  final ServicePost post;
  final VoidCallback onOpen;

  /// "I can help", on someone's request.
  final VoidCallback? onHelp;

  @override
  Widget build(BuildContext context) {
    return Material(
      color: Palette.white,
      borderRadius: BorderRadius.circular(16),
      child: InkWell(
        borderRadius: BorderRadius.circular(16),
        onTap: onOpen,
        child: Padding(
          padding: const EdgeInsets.all(14),
          child: switch (post.kind) {
            ServiceKind.offering => _Offering(post),
            ServiceKind.seeking => _Seeking(post, onHelp: onHelp),
            ServiceKind.hiring => _Job(post),
          },
        ),
      ),
    );
  }
}

class _Offering extends StatelessWidget {
  const _Offering(this.post);

  final ServicePost post;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.stretch,
      children: [
        Row(
          children: [
            InitialsAvatar(author: post.author, radius: 24),
            const SizedBox(width: 12),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Wrap(
                    spacing: 6,
                    crossAxisAlignment: WrapCrossAlignment.center,
                    children: [
                      Text(post.author.displayName, style: text.titleSmall?.copyWith(fontSize: 15.5)),
                      if (post.author.isVerifiedNeighbour) const NeighbourBadge(),
                    ],
                  ),
                  Text(
                    '${post.categoryName} · ${post.title}',
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                    style: text.bodySmall?.copyWith(fontSize: 13),
                  ),
                ],
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Wrap(
          spacing: 14,
          runSpacing: 4,
          children: [
            Text(post.author.rating,
                style: text.bodySmall?.copyWith(fontSize: 13, color: Palette.ink, fontWeight: FontWeight.w600)),
            Text(post.where, style: text.bodySmall?.copyWith(fontSize: 13, color: Palette.inkSoft)),
            if (post.travelKm != null)
              Text('Travels up to ${post.travelKm} km',
                  style: text.bodySmall?.copyWith(fontSize: 13, color: Palette.inkSoft)),
          ],
        ),
        const Padding(padding: EdgeInsets.symmetric(vertical: 10), child: Divider(height: 1, color: Palette.sand)),
        Row(
          children: [
            Expanded(
              child: Text(
                post.rateInfo ?? 'Ask for a quote',
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: text.titleSmall?.copyWith(fontSize: 14),
              ),
            ),
            Container(
              height: 34,
              padding: const EdgeInsets.symmetric(horizontal: 14),
              alignment: Alignment.center,
              decoration: BoxDecoration(color: Palette.glowTint, borderRadius: BorderRadius.circular(17)),
              child: const Text('View',
                  style: TextStyle(color: Palette.primaryDark, fontWeight: FontWeight.w600, fontSize: 13.5)),
            ),
          ],
        ),
      ],
    );
  }
}

class _Seeking extends StatelessWidget {
  const _Seeking(this.post, {this.onHelp});

  final ServicePost post;
  final VoidCallback? onHelp;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text('${post.where} · ${timeAgo(post.createdAt)}', style: text.bodySmall?.copyWith(fontSize: 12.5)),
        const SizedBox(height: 6),
        Text(post.title, style: text.titleSmall?.copyWith(fontSize: 16)),
        if (post.description.isNotEmpty) ...[
          const SizedBox(height: 6),
          Text(post.description,
              maxLines: 3, overflow: TextOverflow.ellipsis, style: text.bodyMedium?.copyWith(color: Palette.inkSoft)),
        ],
        const SizedBox(height: 10),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          crossAxisAlignment: WrapCrossAlignment.center,
          children: [
            _Chip(post.categoryName),
            if (post.budgetInfo != null) _Chip(post.budgetInfo!, strong: true),
          ],
        ),
        if (onHelp != null && !post.isMine) ...[
          const SizedBox(height: 10),
          FilledButton(
            onPressed: onHelp,
            style: FilledButton.styleFrom(minimumSize: const Size(0, 40)),
            child: Text(post.myConversationId == null ? 'I can help' : 'Open chat'),
          ),
        ],
      ],
    );
  }
}

class _Job extends StatelessWidget {
  const _Job(this.post);

  final ServicePost post;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Row(
          children: [
            Expanded(child: Text(post.where, style: text.bodySmall?.copyWith(fontSize: 12.5))),
            Text(timeAgo(post.createdAt), style: text.bodySmall?.copyWith(fontSize: 12.5)),
          ],
        ),
        const SizedBox(height: 6),
        Text(post.title, style: text.titleSmall?.copyWith(fontSize: 16)),
        const SizedBox(height: 8),
        Wrap(
          spacing: 6,
          runSpacing: 6,
          children: [
            if (post.pay != null) _Chip(post.pay!, strong: true),
            if (post.jobTypeName != null) _Chip(post.jobTypeName!),
            _Chip(post.categoryName),
          ],
        ),
        if (post.description.isNotEmpty) ...[
          const SizedBox(height: 8),
          Text(post.description,
              maxLines: 3, overflow: TextOverflow.ellipsis, style: text.bodyMedium?.copyWith(color: Palette.inkSoft)),
        ],
      ],
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(this.label, {this.strong = false});

  final String label;
  final bool strong;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 4),
      decoration: BoxDecoration(
        color: strong ? Palette.glowTint : Palette.cream,
        borderRadius: BorderRadius.circular(13),
      ),
      child: Text(
        label,
        style: TextStyle(
          fontSize: 12.5,
          color: strong ? Palette.primaryDark : Palette.inkSoft,
          fontWeight: strong ? FontWeight.w600 : FontWeight.w400,
        ),
      ),
    );
  }
}

/// The note above the jobs list, and on every job post.
class JobSafetyNote extends StatelessWidget {
  const JobSafetyNote({super.key, this.text});

  /// The server's own wording, when it sent one.
  final String? text;

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(color: Palette.trustTint, borderRadius: BorderRadius.circular(14)),
      child: Row(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Icon(Icons.shield_outlined, size: 20, color: Palette.trustInk),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              text ??
                  'A real employer never asks you to pay to get a job. Posts asking for a fee are held for review.',
              style: Theme.of(context).textTheme.bodySmall?.copyWith(color: Palette.ink, fontSize: 13.5),
            ),
          ),
        ],
      ),
    );
  }
}
