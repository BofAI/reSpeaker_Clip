import 'package:flutter/material.dart';
import 'src/features/desktop/desktop_page.dart';

void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(MaterialApp(
    title: 'Boa Voice',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      useMaterial3: true,
      colorScheme: ColorScheme.fromSeed(seedColor: const Color(0xff167d8d)),
      scaffoldBackgroundColor: const Color(0xfff6f7f9),
      inputDecorationTheme: const InputDecorationTheme(
        border: OutlineInputBorder(),
        isDense: true,
      ),
    ),
    home: const DesktopPage(),
  ));
}
