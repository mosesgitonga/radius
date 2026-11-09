import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/api/api_exception.dart';
import '../../../core/providers.dart';
import '../../../core/router.dart';
import '../feed_controller.dart';
import '../feed_models.dart';

/// Opens the composer on one of your posts. Returns the post as edited, or null
/// if nothing changed. Home's copy is updated here; other screens update theirs.
Future<Post?> editOwnPost(BuildContext context, WidgetRef ref, Post post) async {
  final edited = await context.push<Post>(Routes.editPost(post.id), extra: post);
  if (edited != null) ref.read(feedControllerProvider.notifier).edited(edited);
  return edited;
}

/// Asks, then deletes one of your posts. Returns true once it is gone.
Future<bool> deleteOwnPost(BuildContext context, WidgetRef ref, Post post) async {
  final sure = await showDialog<bool>(
    context: context,
    builder: (dialog) => AlertDialog(
      title: const Text('Delete this post?'),
      content: const Text('It goes for everyone, with its comments. This can’t be undone.'),
      actions: [
        TextButton(onPressed: () => Navigator.of(dialog).pop(false), child: const Text('Keep')),
        TextButton(onPressed: () => Navigator.of(dialog).pop(true), child: const Text('Delete')),
      ],
    ),
  );
  if (sure != true) return false;
  try {
    await ref.read(feedRepositoryProvider).deletePost(post.id);
    ref.read(feedControllerProvider.notifier).removed(post.id);
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Post deleted.')));
    }
    return true;
  } on ApiException catch (error) {
    if (context.mounted) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    return false;
  }
}

/// The Edit and Delete rows for your own post's ⋮ sheet. [onDone] runs after
/// either succeeds, with the edited post, or null once deleted.
List<Widget> ownPostTiles(
  BuildContext sheet,
  BuildContext context,
  WidgetRef ref,
  Post post, {
  required void Function(Post? edited) onDone,
}) =>
    [
      ListTile(
        leading: const Icon(Icons.edit_outlined),
        title: const Text('Edit post'),
        onTap: () async {
          Navigator.of(sheet).pop();
          final edited = await editOwnPost(context, ref, post);
          if (edited != null) onDone(edited);
        },
      ),
      ListTile(
        leading: const Icon(Icons.delete_outline_rounded),
        title: const Text('Delete post'),
        onTap: () async {
          Navigator.of(sheet).pop();
          if (await deleteOwnPost(context, ref, post)) onDone(null);
        },
      ),
    ];
