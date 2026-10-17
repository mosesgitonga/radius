import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/api/api_exception.dart';
import '../../core/providers.dart';
import '../../core/router.dart';

/// Opens the direct conversation with [userId], making it if need be. Nothing
/// is sent until the person writes something.
Future<void> openDirectChat(BuildContext context, WidgetRef ref, String userId) async {
  try {
    final conversation = await ref.read(chatRepositoryProvider).openDirect(userId);
    if (context.mounted) context.go(Routes.conversation(conversation.id));
  } on ApiException catch (error) {
    if (context.mounted) {
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(error.message)));
    }
  }
}
