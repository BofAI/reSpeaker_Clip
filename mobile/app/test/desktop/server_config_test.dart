import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/desktop/server_config.dart';

void main() {
  test('config persists and replaces credentials with owner-only permissions',
      () async {
    final root = await Directory.systemTemp.createTemp('boa-config-test');
    try {
      final file = File('${root.path}/credentials/server.json');
      final config = ServerConfig(file);
      expect(await config.read(), isEmpty);
      await config.save('http://127.0.0.1:8765', 'test-only-token');
      expect((await ServerConfig(file).read())['token'], 'test-only-token');
      expect((await file.stat()).mode & 0x1ff, 0x180);
      expect((await file.parent.stat()).mode & 0x1ff, 0x1c0);
      await config.save('http://localhost:8765', 'replacement-test-token');
      expect((await config.read())['token'], 'replacement-test-token');
      expect(await File('${file.path}.part').exists(), isFalse);
      expect((await file.stat()).mode & 0x1ff, 0x180);
    } finally {
      await root.delete(recursive: true);
    }
  });
}
