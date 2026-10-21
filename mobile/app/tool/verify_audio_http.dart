import 'dart:convert';
import 'dart:io';
import 'package:respeaker_app/src/features/audio_storage/storage_client.dart';

Future<void> main(List<String> args) async {
  if (args.isEmpty) {
    throw ArgumentError(
        'Usage: verify_audio_http.dart <artifacts> [server-data-dir]');
  }
  final root = Directory(args[0]);
  final dataPath = args.length > 1 ? args[1] : '${root.path}/server-data';
  final token = await File('$dataPath/token').readAsString();
  final client = StorageClient('http://127.0.0.1:8765', token.trim());
  final cases = <Map<String, dynamic>>[];
  try {
    final all = await client.list();
    for (final row
        in all.where((r) => (r['filename'] as String).startsWith('tone'))) {
      final dir = Directory('${root.path}/dart-downloads');
      final file = await client.download(row, dir);
      if (await StorageClient.hash(file) != row['sha256']) {
        throw StateError('Hash mismatch');
      }
      final info =
          await client.upload(file, Directory('${root.path}/dart-sessions'));
      if (info['sha256'] != row['sha256']) throw StateError('Upload mismatch');
      final again =
          await client.upload(file, Directory('${root.path}/dart-sessions'));
      if (again['id'] != info['id']) {
        throw StateError('Resume duplicated upload');
      }
      // Convert the verified file into a partial to exercise real Range resume.
      await file.rename('${file.path}.part');
      final partial =
          await File('${file.path}.part').open(mode: FileMode.append);
      await partial.truncate((row['size'] as int) ~/ 2);
      await partial.close();
      final restored = await client.download(row, dir);
      if (await StorageClient.hash(restored) != row['sha256']) {
        throw StateError('Resume hash mismatch');
      }
      cases.add({
        'file': row['filename'],
        'bytes': row['size'],
        'upload_download_resume': true
      });
      stdout.writeln(jsonEncode(cases.last));
    }
    if (cases.map((row) => row['file']).toSet().length < 6) {
      throw StateError('Expected all six generated audio fixtures');
    }
    await File('${root.path}/dart-live-report.json')
        .writeAsString(jsonEncode({'passed': true, 'cases': cases}));
  } finally {
    client.close();
  }
}
