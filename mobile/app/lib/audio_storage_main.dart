import 'package:flutter/material.dart';
import 'src/features/audio_storage/storage_page.dart';

// Explicit development entry point: no official account or backend bootstrap.
void main() {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(MaterialApp(
      debugShowCheckedModeBanner: false,
      title: 'Boa Audio',
      theme: ThemeData(
          colorSchemeSeed: const Color(0xff315847), useMaterial3: true),
      home: const AudioStoragePage()));
}
