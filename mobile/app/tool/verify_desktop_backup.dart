import 'dart:convert';
import 'dart:io';
import 'package:respeaker_app/src/features/desktop/backup_queue.dart';
import 'package:respeaker_app/src/features/desktop/local_library.dart';
import 'package:respeaker_app/src/features/audio_storage/storage_client.dart';

Future<void> main(List<String> args) async {
  final artifacts = Directory(args.single);
  final root = await Directory.systemTemp.createTemp('boa-backup-live');
  final token =
      await File('${artifacts.path}/server-data/token').readAsString();
  const endpoint = 'http://127.0.0.1:8765';
  final client = StorageClient(endpoint, token.trim());
  try {
    final audio = await LocalLibrary(Directory('${root.path}/library'))
        .importFile(File('${artifacts.path}/tone.wav'), name: '桌面自动备份验证.wav');
    final q = BackupQueue(File('${root.path}/outbox.json'));
    await q.load();
    await q.enqueue(audio, endpoint);
    await q.drain((audio, url) async {
      await client.upload(audio.file, Directory('${root.path}/sessions'),
          filename: audio.name);
    });
    if (q.status(audio, endpoint) != '已备份') throw StateError('Backup failed');
    final restored = BackupQueue(q.file);
    await restored.load();
    await restored.drain((_, __) async {
      throw StateError('Completed backup repeated');
    });
    final hash = await StorageClient.hash(audio.file);
    final rows = await client.list();
    if (!rows.any((r) => r['sha256'] == hash && r['filename'] == audio.name)) {
      throw StateError('Missing server copy');
    }
    await File('${artifacts.path}/desktop-auto-backup-report.json')
        .writeAsString(jsonEncode({
      'passed': true,
      'real_http_upload': true,
      'restart_dedup': true,
      'server_sha256': hash
    }));
    stdout.writeln(
        'Automatic queue → real HTTP → server hash → restart dedup: PASS');
  } finally {
    client.close();
    await root.delete(recursive: true);
  }
}
