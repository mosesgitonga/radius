import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../core/providers.dart';
import '../../core/realtime.dart';
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

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
    _realtime = ref.read(realtimeProvider)..start();
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
    if (state == AppLifecycleState.resumed) _realtime.resume();
  }

  @override
  Widget build(BuildContext context) {
    final shell = widget.shell;
    final unread = ref.watch(chatsControllerProvider.select((s) => s.unread));
    return Scaffold(
      body: shell,
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
