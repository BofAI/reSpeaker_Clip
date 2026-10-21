// Development-only automated playback runner. Not used by the normal app.
import 'dart:convert';
import 'dart:io';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import 'src/features/audio_storage/storage_client.dart';
import 'src/features/desktop/local_library.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  runApp(const MaterialApp(
      home: Scaffold(body: Center(child: Text('音频 HTTP / 播放器自动验证中')))));
  final root = await getApplicationDocumentsDirectory();
  final report = <String, dynamic>{
    'passed': false,
    'cases': <Map<String, dynamic>>[]
  };
  final player = AudioPlayer();
  StorageClient? client;
  var offline = false;
  try {
    final configFile = File('${root.path}/audio-storage-smoke.json');
    final config =
        jsonDecode(await configFile.readAsString()) as Map<String, dynamic>;
    await configFile.delete();
    offline = config['offline'] == true;
    List<Map<String, dynamic>> rows;
    final manifest = File('${root.path}/boa-audio-playback-manifest.json');
    if (offline) {
      rows = (jsonDecode(await manifest.readAsString()) as List)
          .cast<Map<String, dynamic>>();
    } else {
      client =
          StorageClient(config['url'] as String, config['token'] as String);
      const vault = FlutterSecureStorage(
        mOptions: MacOsOptions(
            useDataProtectionKeyChain: false, accountName: "boa.voice.desktop"),
      );
      final prefix = Platform.isMacOS ? 'boa.desktop' : 'boa.storage';
      await vault.write(key: '$prefix.address', value: config['url'] as String);
      await vault.write(key: '$prefix.token', value: config['token'] as String);
      if (await vault.read(key: '$prefix.token') != config['token']) {
        throw StateError('Secure storage failed');
      }
      rows = (await client.list())
          .where((r) => (r['filename'] as String).startsWith('tone'))
          .toList();
    }
    for (final row in rows) {
      final file = offline
          ? File(row['local_path'] as String)
          : await client!
              .download(row, Directory('${root.path}/boa_audio/downloads'));
      row['local_path'] = file.path;
      if (await StorageClient.hash(file) != row['sha256']) {
        throw StateError('Local file hash mismatch');
      }
      if (!offline && (row['size'] as int) < 2000000) {
        final uploaded = await client!
            .upload(file, Directory('${root.path}/boa_audio/sessions'));
        if (uploaded['sha256'] != row['sha256']) {
          throw StateError('Re-upload checksum mismatch');
        }
      }
      var playable = file;
      if (Platform.isMacOS) {
        final support = await getApplicationSupportDirectory();
        final library =
            LocalLibrary(Directory('${support.path}/boa_desktop/library'));
        final imported =
            await library.importFile(file, name: row['filename'] as String);
        if (await StorageClient.hash(imported.file) != row['sha256']) {
          throw StateError('Desktop import hash mismatch');
        }
        playable = imported.file;
      }
      final duration = await player.setFilePath(playable.path);
      if (duration == null || duration.inMilliseconds < 3000) {
        throw StateError('Invalid duration');
      }
      final playback = player.play();
      await Future<void>.delayed(const Duration(milliseconds: 500));
      await player.pause();
      if (player.position.inMilliseconds < 100) {
        throw StateError('Playback did not advance');
      }
      await player.seek(Duration(milliseconds: duration.inMilliseconds ~/ 2));
      if ((player.position.inMilliseconds - duration.inMilliseconds ~/ 2)
              .abs() >
          300) {
        throw StateError('Seek mismatch');
      }
      await player.seek(duration - const Duration(milliseconds: 400));
      final ended = player.processingStateStream
          .firstWhere((v) => v == ProcessingState.completed)
          .timeout(const Duration(seconds: 10));
      player.play();
      await ended;
      await playback;
      (report['cases'] as List).add({
        'filename': row['filename'],
        'duration_ms': duration.inMilliseconds,
        'local_play_pause_seek_end': true,
        'download_hash': true,
        'dart_upload': !offline && (row['size'] as int) < 2000000
      });
      await player.stop();
    }
    if ((report['cases'] as List).length < 6) {
      throw StateError('Missing format cases');
    }
    await manifest.writeAsString(jsonEncode(rows), flush: true);
    report['offline'] = offline;
    report['passed'] = true;
  } catch (e) {
    report['error'] = e.toString();
  } finally {
    client?.close();
    await player.dispose();
    await File(
            '${root.path}/audio-storage-${offline ? 'offline' : 'smoke'}-report.json')
        .writeAsString(jsonEncode(report), flush: true);
    runApp(MaterialApp(
        home: Scaffold(
            body: Center(
                child: Text(
                    report['passed'] == true ? '音频验证通过' : '音频验证失败，请查看报告')))));
  }
}
