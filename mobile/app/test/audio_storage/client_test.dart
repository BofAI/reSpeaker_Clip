import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/audio_storage/storage_client.dart';

void main() {
  test('rejects unsafe server addresses before sending credentials', () {
    for (final url in [
      'ftp://localhost',
      'http://u:p@localhost',
      'http://localhost?a=b',
      'http://localhost/#x'
    ]) {
      expect(() => StorageClient(url, 'key'), throwsArgumentError);
    }
  });
  test('does not follow redirect with token', () async {
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((r) {
      r.response.statusCode = 302;
      r.response.headers.set('location', 'http://example.com');
      r.response.close();
    });
    final client = StorageClient('http://127.0.0.1:${server.port}', 'key');
    try {
      await expectLater(client.list(), throwsA(isA<StorageException>()));
    } finally {
      client.close();
      await server.close(force: true);
    }
  });
}
