import 'dart:convert';
import 'dart:io';
import 'local_library.dart';

/// Durable single-worker outbox. Credentials are never persisted here.
class BackupQueue {
  final File file;
  BackupQueue(this.file);
  final Map<String, Map<String, dynamic>> _jobs = {};
  bool running = false;
  Future<void> _writes = Future.value();
  String _key(LocalAudio audio, String endpoint) =>
      '$endpoint|${audio.file.path}';
  Future<void> load() async {
    if (!await file.exists()) return;
    final data = jsonDecode(await file.readAsString()) as Map<String, dynamic>;
    for (final entry in data.entries) {
      final job = Map<String, dynamic>.from(entry.value as Map);
      if (job['state'] == 'uploading') job['state'] = 'pending';
      _jobs[entry.key] = job;
    }
  }

  Future<void> _save() {
    final snapshot = jsonEncode(_jobs);
    _writes = _writes.catchError((Object _) {}).then((_) async {
      await file.parent.create(recursive: true);
      final temp = File('${file.path}.tmp');
      await temp.writeAsString(snapshot, flush: true);
      await temp.rename(file.path);
    });
    return _writes;
  }

  Future<void> enqueue(LocalAudio audio, String endpoint) async {
    final key = _key(audio, endpoint);
    if (_jobs.containsKey(key)) return;
    _jobs[key] = {
      'path': audio.file.path,
      'name': audio.name,
      'bytes': audio.bytes,
      'endpoint': endpoint,
      'state': 'pending',
      'attempts': 0,
      'next': 0
    };
    await _save();
  }

  Future<void> markBackedUp(LocalAudio audio, String endpoint) async {
    await enqueue(audio, endpoint);
    _jobs[_key(audio, endpoint)]!['state'] = 'complete';
    await _save();
  }

  String status(LocalAudio audio, String endpoint) {
    final job = _jobs[_key(audio, endpoint)];
    return switch (job?['state']) {
      'complete' => '已备份',
      'uploading' => '上传中',
      'failed' => (job!['attempts'] as int) >= 3 ? '上传失败，请重试' : '失败，等待重试',
      _ => '待备份',
    };
  }

  Future<void> retryFailed() async {
    for (final job in _jobs.values) {
      if (job['state'] == 'failed') {
        job['attempts'] = 0;
        job['next'] = 0;
        job['state'] = 'pending';
      }
    }
    await _save();
  }

  Future<void> drain(Future<void> Function(LocalAudio, String) upload,
      {bool Function()? allowed,
      void Function()? changed,
      String? endpoint}) async {
    if (running) return;
    running = true;
    try {
      for (final job in _jobs.values.toList()) {
        if (endpoint != null && job['endpoint'] != endpoint) continue;
        if (allowed != null && !allowed()) break;
        if (job['state'] == 'complete' ||
            (job['attempts'] as int) >= 3 ||
            (job['next'] as int) > DateTime.now().millisecondsSinceEpoch) {
          continue;
        }
        job['state'] = 'uploading';
        await _save();
        changed?.call();
        try {
          await upload(
              LocalAudio(File(job['path'] as String), job['name'] as String,
                  job['bytes'] as int),
              job['endpoint'] as String);
          job['state'] = 'complete';
          job['attempts'] = 0;
        } catch (_) {
          job['state'] = 'failed';
          job['attempts'] = (job['attempts'] as int) + 1;
          job['next'] = DateTime.now().millisecondsSinceEpoch +
              30000 * (job['attempts'] as int);
        }
        await _save();
        changed?.call();
      }
    } finally {
      running = false;
      changed?.call();
    }
  }
}
