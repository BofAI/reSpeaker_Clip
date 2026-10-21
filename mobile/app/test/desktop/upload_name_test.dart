import 'dart:convert';
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/audio_storage/storage_client.dart';

void main() {
  test('desktop uploads preserve display name instead of content hash name',
      () async {
    final root = await Directory.systemTemp.createTemp('boa-name-');
    final server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    final client = StorageClient('http://127.0.0.1:${server.port}', 'test');
    addTearDown(() async {
      client.close();
      await server.close(force: true);
      await root.delete(recursive: true);
    });
    final source = File('${root.path}/hash.wav');
    await source.writeAsBytes([1, 2, 3]);
    final serving = () async {
      final request = await server.first;
      final body = jsonDecode(await utf8.decoder.bind(request).join());
      expect(body['filename'], '会议记录.wav');
      expect(body['mime'], 'audio/wav');
      request.response.headers.contentType = ContentType.json;
      request.response
          .write(jsonEncode({'id': 'test-id', 'state': 'complete'}));
      await request.response.close();
    }();
    await client.upload(source, Directory('${root.path}/sessions'),
        filename: '会议记录.wav');
    await serving;
  });
}
