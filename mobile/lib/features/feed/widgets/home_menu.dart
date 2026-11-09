import 'package:flutter/material.dart';
import 'package:go_router/go_router.dart';

import '../../../core/router.dart';
import '../../../core/theme.dart';

/// The menu behind the hamburger on Home: where you are, and the places that
/// belong to your area.
class HomeMenu extends StatelessWidget {
  const HomeMenu({super.key, required this.area, this.radiusKm});

  final String area;
  final double? radiusKm;

  @override
  Widget build(BuildContext context) {
    final text = Theme.of(context).textTheme;
    final radius = radiusKm;

    void open(String route) {
      Navigator.of(context).pop();
      context.go(route);
    }

    return Drawer(
      backgroundColor: Palette.cream,
      child: SafeArea(
        child: ListView(
          padding: EdgeInsets.zero,
          children: [
            Padding(
              padding: const EdgeInsets.fromLTRB(20, 24, 20, 16),
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Icon(Icons.location_on_outlined, color: Palette.primary),
                  const SizedBox(height: 8),
                  Text(area, style: text.headlineSmall),
                  const SizedBox(height: 2),
                  Text(
                    radius == null
                        ? 'Your neighbourhood'
                        : 'Your feed reaches ${radius == radius.roundToDouble() ? radius.toStringAsFixed(0) : radius.toStringAsFixed(1)} km from home',
                    style: text.bodySmall,
                  ),
                ],
              ),
            ),
            Divider(height: 1, color: Palette.line),
            ListTile(
              leading: Icon(Icons.handyman_outlined, color: Palette.ink),
              title: const Text('Services & jobs'),
              subtitle: const Text('Find a fundi, offer a service, or hire'),
              onTap: () => open(Routes.services),
            ),
            ListTile(
              leading: Icon(Icons.people_outline_rounded, color: Palette.ink),
              title: const Text('People nearby'),
              subtitle: const Text('Neighbours who chose to be listed'),
              onTap: () => open(Routes.peopleNearby),
            ),
            ListTile(
              leading: Icon(Icons.groups_outlined, color: Palette.ink),
              title: const Text('Groups'),
              subtitle: const Text('Your estate, church, chama or team'),
              onTap: () => open(Routes.groups),
            ),
          ],
        ),
      ),
    );
  }
}
