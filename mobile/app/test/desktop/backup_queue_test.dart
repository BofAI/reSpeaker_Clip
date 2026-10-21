import 'dart:io';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/desktop/backup_queue.dart';
import 'package:respeaker_app/src/features/desktop/local_library.dart';

void main() {
  test('backup is durable, retries failures and does not repeat completed jobs',
      () async {
    final dir = await Directory.systemTemp.createTemp('backup-test');
    addTearDown(() => dir.delete(recursive: true));
    final f = File('${dir.path}/a.wav');
    await f.writeAsBytes([1, 2]);
    final audio = LocalAudio(f, 'a.wav', 2);
    final q = BackupQueue(File('${dir.path}/queue.json'));
    await q.load();
    await q.enqueue(audio, 'http://localhost:8765');
    await q.drain((_, __) async {
      throw const SocketException('offline');
    });
    expect(q.status(audio, 'http://localhost:8765'), '失败，等待重试');
    final restored = BackupQueue(q.file);
    await restored.load();
    await restored.retryFailed();
    var calls = 0;
    await restored.drain((_, __) async {
      calls++;
    });
    await restored.enqueue(audio, 'http://localhost:8765');
    await restored.drain((_, __) async {
      calls++;
    });
    expect(calls, 1);
    expect(restored.status(audio, 'http://localhost:8765'), '已备份');
    await restored.enqueue(audio, 'http://localhost:9999');
    await restored.drain((_, __) async {
      calls++;
    });
    expect(calls, 2);
  });
  test(
      'disabled queue does not upload and failed tasks stop after three attempts',
      () async {
    final dir = await Directory.systemTemp.createTemp('backup-limit');
    addTearDown(() => dir.delete(recursive: true));
    final f = File('${dir.path}/audio.wav');
    await f.writeAsBytes([1]);
    final audio = LocalAudio(f, 'audio.wav', 1);
    final file = File('${dir.path}/queue.json');
    var q = BackupQueue(file);
    await q.load();
    await q.enqueue(audio, 'http://a');
    var calls = 0;
    Future<void> fail(LocalAudio _, String __) async {
      calls++;
      throw const SocketException('offline');
    }

    await q.drain(fail, allowed: () => false);
    expect(calls, 0);
    for (var i = 0; i < 4; i++) {
      await q.drain(fail);
      final json = jsonDecode(await file.readAsString()) as Map;
      for (final job in json.values) {
        job['next'] = 0;
      }
      await file.writeAsString(jsonEncode(json));
      q = BackupQueue(file);
      await q.load();
    }
    expect(calls, 3);
    expect(q.status(audio, 'http://a'), '上传失败，请重试');
    await q.retryFailed();
    await q.drain((_, __) async {
      calls++;
    });
    expect(calls, 4);
    expect(q.status(audio, 'http://a'), '已备份');
  });
}
