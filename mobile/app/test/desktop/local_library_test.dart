import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/desktop/local_library.dart';

void main() {
  test('import persists names, preserves source and deduplicates content',
      () async {
    final temp = await Directory.systemTemp.createTemp('boa-library-');
    addTearDown(() => temp.delete(recursive: true));
    final source = File('${temp.path}/meeting.wav');
    await source.writeAsBytes([1, 2, 3, 4]);
    final library = LocalLibrary(Directory('${temp.path}/library'));
    final imported = await library.importFile(source);
    expect(imported.name, 'meeting.wav');
    expect(await source.exists(), isTrue);
    expect(await imported.file.readAsBytes(), [1, 2, 3, 4]);
    expect((await library.importFile(source)).file.path, imported.file.path);
    final restored = await LocalLibrary(library.root).list();
    expect(restored.length, 1);
    expect(restored.single.name, 'meeting.wav');
    await File('${library.root.path}/incomplete.part').writeAsString('partial');
    expect((await library.list()).length, 1);
  });
  test('unsupported and empty imports leave library unchanged', () async {
    final temp = await Directory.systemTemp.createTemp('boa-library-');
    addTearDown(() => temp.delete(recursive: true));
    final library = LocalLibrary(Directory('${temp.path}/library'));
    final source = File('${temp.path}/empty.wav');
    await source.writeAsBytes([]);
    await expectLater(
        library.importFile(source), throwsA(isA<FormatException>()));
    final other = File('${temp.path}/secret.txt');
    await other.writeAsString('not audio');
    await expectLater(
        library.importFile(other), throwsA(isA<FormatException>()));
    expect(await library.list(), isEmpty);
  });
}
