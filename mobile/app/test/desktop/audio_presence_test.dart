import 'dart:io';
import 'dart:convert';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/desktop/local_library.dart';
import 'package:respeaker_app/src/features/desktop/audio_presence.dart';

void main() {
  test('location uses content and size, not filename; offline is unknown', () {
    final local = LocalAudio(File('/library/abc.wav'), 'meeting.wav', 99);
    final remote = {'sha256': 'abc', 'size': 99, 'filename': 'different.wav'};
    expect(localPresence(local, [remote], connected: true), '本地 + 服务器');
    expect(localPresence(local, [], connected: true), '仅本地');
    expect(localPresence(local, [remote], connected: false), '本地 · 服务器状态未确认');
    expect(remotePresence(remote, [local]), '本地 + 服务器');
    expect(remotePresence({...remote, 'size': 100}, [local]), '仅服务器');
    expect(remoteTime({'created': 1700000000.5}).millisecondsSinceEpoch,
        1700000000500);
  });
  test(
      'library sorts newest first and preserves import time across restart and duplicate',
      () async {
    final root = await Directory.systemTemp.createTemp('boa-order-');
    addTearDown(() => root.delete(recursive: true));
    final library = LocalLibrary(Directory('${root.path}/library'));
    final source = File('${root.path}/source.wav');
    await source.writeAsBytes([1]);
    final first = await library.importFile(source, name: 'a.wav');
    await File('${first.file.path}.json').writeAsString(
        jsonEncode({'name': 'a.wav', 'addedAt': '2020-01-01T00:00:00Z'}));
    await source.writeAsBytes([2]);
    final second = await library.importFile(source, name: 'z.wav');
    final rows = await LocalLibrary(library.root).list();
    expect(rows.map((r) => r.name), ['z.wav', 'a.wav']);
    final duplicate = await library.importFile(source);
    expect(duplicate.addedAt, second.addedAt);
    expect(rows.last.addedAt, DateTime.utc(2020));
  });
}
