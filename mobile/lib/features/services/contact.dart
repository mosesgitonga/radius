import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/router.dart';
import '../me/contact_gate.dart';
import 'service_models.dart';

/// Opens the conversation with [post]'s author about it, made if need be. As
/// with any first contact, it is a request until they reply.
Future<void> contactAuthor(BuildContext context, WidgetRef ref, ServicePost post) async {
  if (post.myConversationId == null && !await mayContactStrangers(context, ref)) return;
  try {
    final id = post.myConversationId ?? (await ref.read(serviceRepositoryProvider).contact(post.id)).id;
    if (context.mounted) context.go(Routes.conversation(id));
  } on ApiException catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    }
  }
}
