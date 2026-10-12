import 'dart:io';

import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

/// Per-account Documents paths (SQLite shard + synced recording files).
class AccountStoragePaths {
  AccountStoragePaths._();

  static String sanitizeAccountKey(String raw) =>
      raw.trim().replaceAll(RegExp(r'[^a-zA-Z0-9_\-]'), '');

  static String _requireSafeKey(String accountKey) {
    final safe = sanitizeAccountKey(accountKey);
    if (safe.isEmpty) {
      throw ArgumentError.value(
        accountKey,
        'accountKey',
        'must be non-empty after sanitization',
      );
    }
    return safe;
  }

  static Future<String> documentsRoot() async {
    return (await getApplicationDocumentsDirectory()).path;
  }

  /// Synced / device recordings: `recordings_{accountKey}/…`
  static Future<String> recordingsRoot(String accountKey) async {
    final docs = await documentsRoot();
    final safe = _requireSafeKey(accountKey);
    return p.join(docs, 'recordings_$safe');
  }

  static Future<String> deviceSessionDirectory({
    required String accountKey,
    required String deviceId,
    required String sessionId,
  }) async {
    return p.join(
      await recordingsRoot(accountKey),
      'device',
      deviceId,
      sessionId,
    );
  }

  static Future<String> deviceSessionOpusFile({
    required String accountKey,
    required String deviceId,
    required String sessionId,
  }) async {
    return p.join(
      await recordingsRoot(accountKey),
      'device',
      deviceId,
      '$sessionId.opus',
    );
  }

  /// Local-only recorder / imports under the same account namespace.
  static Future<String> localRecordingsDirectory(String accountKey) async {
    return recordingsRoot(accountKey);
  }

  static Future<String> trimmedDirectory(String accountKey) async {
    final docs = await documentsRoot();
    final safe = _requireSafeKey(accountKey);
    return p.join(docs, 'trimmed_$safe');
  }

  /// Rewrites pre-unified paths (`…/recordings/device/…`) to `…/recordings_{key}/device/…`.
  static String? rewriteLegacyRecordingPath(String? path, String accountKey) {
    if (path == null) return null;
    final trimmed = path.trim();
    if (trimmed.isEmpty) return path;
    final safe = sanitizeAccountKey(accountKey);
    if (safe.isEmpty) return path;

    const legacySeg = '/recordings/';
    final accountSeg = '/recordings_$safe/';
    if (trimmed.contains(accountSeg)) return path;

    final idx = trimmed.indexOf(legacySeg);
    if (idx < 0) return path;

    return trimmed.replaceFirst(legacySeg, accountSeg);
  }

  /// Rewrites a stored absolute path onto the current app Documents root.
  ///
  /// iOS overlay installs often keep files but change the container UUID
  /// (`…/Application/<uuid>/Documents/…`). DB rows still hold the old UUID.
  static String remapToCurrentDocumentsRoot(
    String storedPath,
    String documentsRoot,
  ) {
    final trimmed = storedPath.trim();
    if (trimmed.isEmpty) return storedPath;
    final docs = documentsRoot.trim();
    if (docs.isEmpty) return trimmed;

    final storedNorm = p.normalize(trimmed);
    final docsNorm = p.normalize(docs);

    if (p.equals(storedNorm, docsNorm) || p.isWithin(docsNorm, storedNorm)) {
      return storedNorm;
    }

    const documentsMarker = '/Documents/';
    final docsIdx = storedNorm.lastIndexOf(documentsMarker);
    if (docsIdx >= 0) {
      final relative = storedNorm.substring(docsIdx + documentsMarker.length);
      if (relative.isNotEmpty) {
        return p.normalize(p.join(docsNorm, relative));
      }
    }

    for (final marker in [
      '/recordings_',
      '/recordings/',
      '/trimmed_',
      '/trimmed/',
    ]) {
      final idx = storedNorm.lastIndexOf(marker);
      if (idx >= 0) {
        return p.normalize(p.join(docsNorm, storedNorm.substring(idx + 1)));
      }
    }

    if (storedNorm.startsWith('recordings_') ||
        storedNorm.startsWith('recordings/') ||
        storedNorm.startsWith('trimmed_') ||
        storedNorm.startsWith('trimmed/')) {
      return p.normalize(p.join(docsNorm, storedNorm));
    }

    return storedNorm;
  }

  /// Ordered rewrite candidates for a stored recording/sidecar path.
  ///
  /// First existing file wins at startup: original, legacy dir rename, then
  /// current-sandbox remaps. Does not check the filesystem.
  static List<String> localPathRewriteCandidates({
    required String storedPath,
    required String accountKey,
    required String documentsRoot,
  }) {
    final seen = <String>{};
    final out = <String>[];
    void add(String? value) {
      final trimmed = value?.trim();
      if (trimmed == null || trimmed.isEmpty) return;
      final norm = p.normalize(trimmed);
      if (seen.add(norm)) out.add(norm);
    }

    add(storedPath);
    final legacy = rewriteLegacyRecordingPath(storedPath, accountKey);
    add(legacy);
    add(remapToCurrentDocumentsRoot(storedPath, documentsRoot));
    add(remapToCurrentDocumentsRoot(legacy ?? storedPath, documentsRoot));
    return out;
  }

  /// Moves files from unified-era `recordings/` and `trimmed/` into per-account dirs.
  ///
  /// Skips when the destination file already exists. Returns count of files moved.
  static Future<int> migrateLegacyFilesystemDirs(String accountKey) async {
    final safe = sanitizeAccountKey(accountKey);
    if (safe.isEmpty) return 0;

    final docs = await documentsRoot();
    var moved = 0;
    moved += await _migrateDirTree(
      legacyRoot: p.join(docs, 'recordings'),
      targetRoot: p.join(docs, 'recordings_$safe'),
    );
    moved += await _migrateDirTree(
      legacyRoot: p.join(docs, 'trimmed'),
      targetRoot: p.join(docs, 'trimmed_$safe'),
    );
    return moved;
  }

  static Future<int> _migrateDirTree({
    required String legacyRoot,
    required String targetRoot,
  }) async {
    if (legacyRoot == targetRoot) return 0;

    final legacy = Directory(legacyRoot);
    if (!await legacy.exists()) return 0;

    var moved = 0;
    await for (final entity in legacy.list(recursive: true, followLinks: false)) {
      if (entity is! File) continue;
      final relative = p.relative(entity.path, from: legacyRoot);
      final destPath = p.join(targetRoot, relative);
      final destFile = File(destPath);
      if (await destFile.exists()) continue;
      await Directory(p.dirname(destPath)).create(recursive: true);
      try {
        await entity.rename(destPath);
        moved++;
      } catch (_) {
        try {
          await destFile.writeAsBytes(await entity.readAsBytes());
          await entity.delete();
          moved++;
        } catch (_) {}
      }
    }

    if (moved > 0) {
      try {
        final hasFiles = await legacy
            .list(recursive: true, followLinks: false)
            .any((e) => e is File);
        if (!hasFiles) {
          await legacy.delete(recursive: true);
        }
      } catch (_) {}
    }
    return moved;
  }
}
