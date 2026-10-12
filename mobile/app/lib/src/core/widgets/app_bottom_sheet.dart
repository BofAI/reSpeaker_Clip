import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../app/theme/app_colors.dart';
import '../../app/theme/app_radii.dart';
const double kAppSheetTopGap = 120.0;

class AppSheetDragHandle extends StatelessWidget {
  const AppSheetDragHandle({super.key});

  @override
  Widget build(BuildContext context) {
    return Center(
      child: Container(
        width: 44,
        height: 5,
        decoration: BoxDecoration(
          color: AppColors.gray200,
          borderRadius: BorderRadius.circular(AppRadii.pill),
        ),
      ),
    );
  }
}

class AppBottomSheetShell extends StatelessWidget {
  final Widget child;
  final bool showDragHandle;

  const AppBottomSheetShell({
    super.key,
    required this.child,
    this.showDragHandle = true,
  });

  @override
  Widget build(BuildContext context) {
    // Draw the sheet behind the home indicator, but keep buttons above both
    // that inset and the system back-gesture zone. On some phones the gesture
    // zone is taller than viewPadding; a tap there closes the sheet and the
    // page under it, which leaves a black screen.
    final bottomInset = math.max(
      MediaQuery.viewPaddingOf(context).bottom,
      MediaQuery.systemGestureInsetsOf(context).bottom,
    );
    return ClipRRect(
      borderRadius: const BorderRadius.vertical(top: Radius.circular(AppRadii.r28)),
      child: Material(
        color: AppColors.surface,
        surfaceTintColor: Colors.transparent,
        child: Padding(
          padding: EdgeInsets.only(bottom: bottomInset),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              if (showDragHandle) ...[
                const SizedBox(height: 16),
                const AppSheetDragHandle(),
                const SizedBox(height: 18),
              ],
              Flexible(fit: FlexFit.loose, child: child),
            ],
          ),
        ),
      ),
    );
  }
}

Future<T?> showAppBottomSheet<T>(
  BuildContext context, {
  required WidgetBuilder builder,
  bool useRootNavigator = true,
  double topGap = kAppSheetTopGap,
  bool showDragHandle = true,
  Color? barrierColor,
  bool isDismissible = true,
  /// Animation duration; default 150ms for snappy sheets
  Duration? transitionDuration,
}) {
  final h = MediaQuery.sizeOf(context).height;
  final duration = transitionDuration ?? const Duration(milliseconds: 150);
  var popped = false;
  return showModalBottomSheet<T>(
    context: context,
    useRootNavigator: useRootNavigator,
    isScrollControlled: true,
    isDismissible: isDismissible,
    enableDrag: isDismissible,
    backgroundColor: Colors.transparent,
    barrierColor: barrierColor,
    sheetAnimationStyle: AnimationStyle(
      duration: duration,
      reverseDuration: duration,
    ),
    builder: (ctx) {
      final nav = Navigator.of(ctx, rootNavigator: useRootNavigator);
      void dismiss() {
        if (popped || !nav.mounted || !nav.canPop()) return;
        final route = ModalRoute.of(ctx);
        // A bottom-edge tap on some Android phones is both the button and a
        // system back. Pop only while this sheet is still the current route.
        if (route == null || !route.isCurrent) return;
        popped = true;
        nav.pop();
      }

      final keyboardInset = MediaQuery.viewInsetsOf(ctx).bottom;
      final panel = ConstrainedBox(
        constraints: BoxConstraints(
          maxHeight: math.max(0, h - topGap - keyboardInset),
        ),
        child: AppBottomSheetShell(
          showDragHandle: showDragHandle,
          child: builder(ctx),
        ),
      );

      // canPop stays false so predictive back does not also pop the page
      // under the sheet. One explicit pop closes only this route.
      return PopScope(
        canPop: false,
        onPopInvokedWithResult: (didPop, _) {
          if (didPop || !isDismissible) return;
          dismiss();
        },
        child: panel,
      );
    },
  );
}

