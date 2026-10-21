import 'dart:convert';
import 'dart:io';
import '../audio_storage/storage_client.dart';

class LocalAudio {
  final File file;
  final String name;
  final int bytes;
  final DateTime addedAt;
  LocalAudio(this.file, this.name, this.bytes, {DateTime? addedAt})
      : addedAt = addedAt ?? DateTime.fromMillisecondsSinceEpoch(0);
}

/// Private app copies survive file-picker grants and source moves on macOS.
class LocalLibrary {
  final Directory root;
  LocalLibrary(this.root);

  Future<LocalAudio> importFile(File source, {String? name}) async {
    final title = name ?? source.uri.pathSegments.last;
    final extension = title.split('.').last.toLowerCase();
    if (!['wav', 'm4a', 'mp3', 'ogg', 'flac', 'aac'].contains(extension)) {
      throw const FormatException('请选择 WAV、M4A、MP3、Ogg、FLAC 或 AAC 音频');
    }
    final size = await source.length();
    if (size == 0) throw const FormatException('不能导入空文件');
    await root.create(recursive: true);
    final hash = await StorageClient.hash(source);
    final target = File('${root.path}/$hash.$extension');
    if (await target.exists()) {
      if (await StorageClient.hash(target) == hash) {
        final records = await list();
        return records.firstWhere((r) => r.file.path == target.path,
            orElse: () => LocalAudio(target, title, size));
      }
    }
    final partial = File('${target.path}.part');
    try {
      await source.copy(partial.path);
      if (await StorageClient.hash(partial) != hash) {
        throw const FormatException('导入期间文件发生变化，请重试');
      }
      await partial.rename(target.path);
      final addedAt = DateTime.now().toUtc();
      final meta = File('${target.path}.json.part');
      await meta.writeAsString(
          jsonEncode({'name': title, 'addedAt': addedAt.toIso8601String()}),
          flush: true);
      await meta.rename('${target.path}.json');
      return LocalAudio(target, title, size, addedAt: addedAt);
    } finally {
      if (await partial.exists()) await partial.delete();
    }
  }

  Future<List<LocalAudio>> list() async {
    if (!await root.exists()) return [];
    final rows = <LocalAudio>[];
    await for (final entity in root.list()) {
      if (entity is! File ||
          !RegExp(r'^[a-f0-9]{64}\.(wav|m4a|mp3|ogg|flac|aac)$')
              .hasMatch(entity.uri.pathSegments.last)) {
        continue;
      }
      var name = entity.uri.pathSegments.last;
      var addedAt = (await entity.stat()).modified;
      try {
        final metadata =
            jsonDecode(await File('${entity.path}.json').readAsString());
        if (metadata['name'] is String) name = metadata['name'] as String;
        addedAt =
            DateTime.tryParse(metadata['addedAt']?.toString() ?? '') ?? addedAt;
      } on FileSystemException {
        // A committed audio file remains available if metadata was interrupted.
      } on FormatException {
        // Keep playable bytes accessible even if a sidecar is damaged.
      }
      rows.add(
          LocalAudio(entity, name, await entity.length(), addedAt: addedAt));
    }
    rows.sort((a, b) {
      final time = b.addedAt.compareTo(a.addedAt);
      return time != 0 ? time : a.file.path.compareTo(b.file.path);
    });
    return rows;
  }
}
