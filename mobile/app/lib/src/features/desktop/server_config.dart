import 'dart:convert';
import 'dart:io';

/// Local-use credentials. Plaintext in the app sandbox, owner-only on macOS.
class ServerConfig {
  ServerConfig(this.file);
  final File file;

  Future<void> _protect(String path, String mode) async {
    if (!Platform.isMacOS && !Platform.isLinux) {
      throw UnsupportedError('本地凭据文件目前仅支持 macOS/Linux');
    }
    final result = await Process.run('/bin/chmod', [mode, path]);
    if (result.exitCode != 0) {
      throw const FileSystemException('无法设置配置文件权限');
    }
  }

  Future<Map<String, String>> read() async {
    if (!await file.exists()) return {};
    await _protect(file.path, '600');
    final data = jsonDecode(await file.readAsString()) as Map;
    return {
      'address': data['address'] as String,
      'token': data['token'] as String
    };
  }

  Future<void> save(String address, String token) async {
    await file.parent.create(recursive: true);
    await _protect(file.parent.path, '700');
    final temp = File('${file.path}.part');
    try {
      await temp.create();
      await _protect(temp.path, '600');
      await temp.writeAsString(jsonEncode({'address': address, 'token': token}),
          flush: true);
      await temp.rename(file.path);
    } finally {
      if (await temp.exists()) await temp.delete();
    }
  }
}
