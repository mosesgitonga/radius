import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/location.dart';
import '../../core/providers.dart';
import '../../core/push.dart';
import '../../core/realtime.dart';
import '../../core/router.dart';
import '../../core/theme.dart';
import '../auth/auth_controller.dart';
import '../chats/chats_controller.dart';

/// The signed-in frame: the four tabs, each keeping its own history.
///
/// It also holds the realtime socket open for as long as someone is signed in,
/// and reconnects it when the app comes back to the foreground.
class ShellScreen extends ConsumerStatefulWidget {
  const ShellScreen({super.key, required this.shell, this.showTabs = true});

  final StatefulNavigationShell shell;

  /// Hidden inside a conversation, where the message box needs the room.
  final bool showTabs;

  @override
  ConsumerState<ShellScreen> createState() => _ShellScreenState();
}

class _ShellScreenState extends ConsumerState<ShellScreen> with WidgetsBindingObserver {
  late final RealtimeClient _realtime;
  bool _checking = false;

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _realtime = ref.read(realtimeProvider)..start();
    Future.microtask(_dailyCheck);
    Future.microtask(() => ref.read(pushServiceProvider).start(ref.read(routerProvider)));
  }

  /// Once a day, while the app is open: is the phone at home? Only when location
  /// is already allowed — it never asks — and silent on any failure, trying again
  /// the next time the app opens. Decision M.7.
  Future<void> _dailyCheck() async {
    final auth = ref.read(authControllerProvider);
    if (_checking || auth is! AuthSignedIn || !auth.me.presenceCheckDue) return;
    _checking = true;
    try {
      final reading = await const LocationReader().readHere(mayAsk: false);
      await ref.read(authRepositoryProvider).checkPresence(reading);
      await ref.read(authControllerProvider.notifier).refreshMe();
    } catch (_) {
      // No location, no connection, or not at home's standard of accuracy today.
    } finally {
      _checking = false;
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    // Signed out: the socket closes with the shell.
    _realtime.stop();
    super.dispose();
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (state == AppLifecycleState.resumed) {
      _realtime.resume();
      _dailyCheck();
    }
  }

  @override
  Widget build(BuildContext context) {
    final shell = widget.shell;
    final unread = ref.watch(chatsControllerProvider.select((s) => s.unread));
    final auth = ref.watch(authControllerProvider);
    final deletionPending = auth is AuthSignedIn && auth.me.deletionPending;
    return Scaffold(
      body: deletionPending
          ? Column(
              children: [
                // Signed in during the grace period: nothing can be posted until
                // the deletion is cancelled, so say so on every tab.
                Material(
                  color: Palette.glowTint,
                  child: SafeArea(
                    bottom: false,
                    child: ListTile(
                      dense: true,
                      leading: const Icon(Icons.warning_amber_rounded, color: Palette.primaryDark),
                      title: const Text('Your account is due to be deleted'),
                      trailing: TextButton(onPressed: () => context.go(Routes.me), child: const Text('Review')),
                    ),
                  ),
                ),
                Expanded(child: MediaQuery.removePadding(context: context, removeTop: true, child: shell)),
              ],
            )
          : shell,
      bottomNavigationBar: !widget.showTabs
          ? null
          : NavigationBar(
              selectedIndex: shell.currentIndex,
              onDestinationSelected: (index) =>
                  shell.goBranch(index, initialLocation: index == shell.currentIndex),
              destinations: [
                const NavigationDestination(
                  icon: Icon(Icons.home_outlined),
                  selectedIcon: Icon(Icons.home_rounded),
                  label: 'Home',
                ),
                const NavigationDestination(
                  icon: Icon(Icons.shopping_bag_outlined),
                  selectedIcon: Icon(Icons.shopping_bag_rounded),
                  label: 'Market',
                ),
                NavigationDestination(
                  icon: Badge(
                    isLabelVisible: unread > 0,
                    label: Text(unread > 99 ? '99+' : '$unread'),
                    child: const Icon(Icons.chat_bubble_outline_rounded),
                  ),
                  selectedIcon: Badge(
                    isLabelVisible: unread > 0,
                    label: Text(unread > 99 ? '99+' : '$unread'),
                    child: const Icon(Icons.chat_bubble_rounded),
                  ),
                  label: 'Chats',
                ),
                const NavigationDestination(
                  icon: Icon(Icons.person_outline_rounded),
                  selectedIcon: Icon(Icons.person_rounded),
                  label: 'Me',
                ),
              ],
            ),
    );
  }
}
