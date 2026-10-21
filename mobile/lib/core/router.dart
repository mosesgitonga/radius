import 'package:flutter/foundation.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../features/auth/auth_controller.dart';
import '../features/auth/screens/code_screen.dart';
import '../features/auth/screens/home_area_screen.dart';
import '../features/auth/screens/phone_screen.dart';
import '../features/auth/screens/signup_screen.dart';
import '../features/auth/screens/status_screens.dart';
import '../features/chats/screens/chats_screen.dart';
import '../features/chats/screens/conversation_screen.dart';
import '../features/feed/screens/compose_screen.dart';
import '../features/feed/screens/feed_screen.dart';
import '../features/feed/screens/post_screen.dart';
import '../features/me/me_screen.dart';
import '../features/services/screens/my_services_screen.dart';
import '../features/services/screens/service_compose_screen.dart';
import '../features/services/screens/service_post_screen.dart';
import '../features/services/screens/services_screen.dart';
import '../features/services/service_models.dart';
import '../features/shell/shell_screen.dart';
import '../features/shell/tab_placeholder.dart';

class Routes {
  const Routes._();

  static const restoring = '/';
  static const offline = '/offline';
  static const signIn = '/sign-in';
  static const code = '/sign-in/code';
  static const signUp = '/sign-up';
  static const homeArea = '/home-area';
  static const feed = '/feed';
  static const market = '/market';
  static const chats = '/chats';
  static const me = '/me';
  static const compose = '/compose';
  static String post(String id) => '/feed/post/$id';
  static String conversation(String id) => '/chats/c/$id';
  static const services = '/feed/services';
  static const myServices = '/feed/services/mine';
  static String service(String id) => '/feed/services/p/$id';
  static String newService(ServiceKind kind) => '/services/new?kind=${kind.key}';
  static String editService(String id) => '/services/$id/edit';

  static const _signedOut = {signIn, code, signUp};
  static const _gates = {restoring, offline, signIn, code, signUp, homeArea};
}

/// Decides where someone may be, from their [AuthState] alone. Pure, so it is
/// tested directly.
String? redirectFor(AuthState auth, String location) {
  return switch (auth) {
    AuthRestoring() => location == Routes.restoring ? null : Routes.restoring,
    AuthOffline() => location == Routes.offline ? null : Routes.offline,
    AuthSignedOut() => Routes._signedOut.contains(location) ? null : Routes.signIn,
    AuthNeedsHome() => location == Routes.homeArea ? null : Routes.homeArea,
    AuthSignedIn() => Routes._gates.contains(location) ? Routes.feed : null,
  };
}

final routerProvider = Provider<GoRouter>((ref) {
  // The router reads auth state on each redirect and is told when it changes.
  final refresh = ValueNotifier<AuthState>(ref.read(authControllerProvider));
  ref.listen<AuthState>(authControllerProvider, (_, next) => refresh.value = next);
  ref.onDispose(refresh.dispose);

  return GoRouter(
    initialLocation: Routes.restoring,
    refreshListenable: refresh,
    redirect: (context, state) => redirectFor(refresh.value, state.matchedLocation),
    routes: [
      GoRoute(path: Routes.restoring, builder: (_, _) => const RestoringScreen()),
      GoRoute(path: Routes.offline, builder: (_, _) => const OfflineScreen()),
      GoRoute(path: Routes.signIn, builder: (_, _) => const PhoneScreen()),
      GoRoute(path: Routes.code, builder: (_, _) => const CodeScreen()),
      GoRoute(path: Routes.signUp, builder: (_, _) => const SignupScreen()),
      GoRoute(path: Routes.homeArea, builder: (_, _) => const HomeAreaScreen()),
      GoRoute(path: Routes.compose, builder: (_, _) => const ComposeScreen()),
      GoRoute(
        path: '/services/new',
        builder: (_, state) => ServiceComposeScreen(
          kind: ServiceKind.values.firstWhere(
            (k) => k.key == state.uri.queryParameters['kind'],
            orElse: () => ServiceKind.offering,
          ),
        ),
      ),
      GoRoute(
        path: '/services/:id/edit',
        builder: (_, state) => ServiceComposeScreen(editing: state.extra as ServicePost?),
      ),
      StatefulShellRoute.indexedStack(
        builder: (context, state, shell) => ShellScreen(
          shell: shell,
          showTabs: !state.uri.path.startsWith('/chats/c/'),
        ),
        branches: [
          StatefulShellBranch(routes: [
            GoRoute(
              path: Routes.feed,
              builder: (_, _) => const FeedScreen(),
              routes: [
                GoRoute(
                  path: 'post/:id',
                  builder: (_, state) => PostScreen(postId: state.pathParameters['id']!),
                ),
                GoRoute(
                  path: 'services',
                  builder: (_, _) => const ServicesScreen(),
                  routes: [
                    GoRoute(path: 'mine', builder: (_, _) => const MyServicesScreen()),
                    GoRoute(
                      path: 'p/:id',
                      // A fresh `extra` asks for a fresh page: the Home tab may still hold
                      // an older copy of this post.
                      builder: (_, state) => ServicePostScreen(
                        key: ValueKey('${state.pathParameters['id']}:${state.extra}'),
                        postId: state.pathParameters['id']!,
                      ),
                    ),
                  ],
                ),
              ],
            ),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(
              path: Routes.market,
              builder: (_, _) => const TabPlaceholder(
                title: 'Marketplace',
                message: 'Buying and selling nearby arrives in a later build.',
              ),
            ),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(
              path: Routes.chats,
              builder: (_, _) => const ChatsScreen(),
              routes: [
                GoRoute(
                  path: 'c/:id',
                  builder: (_, state) => ConversationScreen(conversationId: state.pathParameters['id']!),
                ),
              ],
            ),
          ]),
          StatefulShellBranch(routes: [
            GoRoute(path: Routes.me, builder: (_, _) => const MeScreen()),
          ]),
        ],
      ),
    ],
  );
});
