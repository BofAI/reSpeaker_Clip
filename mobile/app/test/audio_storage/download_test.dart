import 'dart:io';
import 'package:crypto/crypto.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/audio_storage/storage_client.dart';

void main() {
  test('server ignoring Range replaces partial instead of appending', () async {
    final bytes = List<int>.generate(20000, (i) => i % 251);
    final hash = sha256.convert(bytes).toString();
    final root = await Directory.systemTemp.createTemp('boa-download');
    final id = 'a' * 32;
    await File('${root.path}/$id.wav.part')
        .writeAsBytes(bytes.take(8000).toList());
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    var sawRange = false;
    server.listen((request) async {
      sawRange = request.headers.value('range') == 'bytes=8000-';
      request.response.headers.set('etag', '"$hash"');
      request.response.add(bytes);
      await request.response.close();
    });
    final client = StorageClient('http://127.0.0.1:${server.port}', 'test');
    try {
      final f = await client.download(
          {'id': id, 'size': bytes.length, 'sha256': hash, 'mime': 'audio/wav'},
          root);
      expect(sawRange, isTrue);
      expect(await f.readAsBytes(), bytes);
    } finally {
      client.close();
      await server.close(force: true);
      await root.delete(recursive: true);
    }
  });
  test('bad content never becomes a playable local file', () async {
    final root = await Directory.systemTemp.createTemp('boa-corrupt');
    final id = 'b' * 32;
    final expected = sha256.convert([1, 2, 3]).toString();
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) async {
      request.response.headers.set('etag', '"$expected"');
      request.response.add([3, 2, 1]);
      await request.response.close();
    });
    final client = StorageClient('http://127.0.0.1:${server.port}', 'test');
    try {
      await expectLater(
          client.download(
              {'id': id, 'size': 3, 'sha256': expected, 'mime': 'audio/wav'},
              root),
          throwsA(isA<StorageException>()));
      expect(await File('${root.path}/$id.wav').exists(), isFalse);
      expect(await File('${root.path}/$id.wav.part').exists(), isFalse);
    } finally {
      client.close();
      await server.close(force: true);
      await root.delete(recursive: true);
    }
  });
}
