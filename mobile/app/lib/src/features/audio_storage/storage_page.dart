import 'dart:io';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import 'storage_client.dart';

class AudioStoragePage extends StatefulWidget {
  const AudioStoragePage({super.key});
  @override
  State<AudioStoragePage> createState() => _AudioStoragePageState();
}

class _AudioStoragePageState extends State<AudioStoragePage> {
  final _address = TextEditingController(text: 'http://127.0.0.1:8765');
  final _token = TextEditingController();
  final _vault = const FlutterSecureStorage();
  final _player = AudioPlayer();
  StorageClient? _client;
  List<Map<String, dynamic>> _items = [];
  List<File> _local = [];
  bool _busy = false;
  double _progress = 0;
  String _message = '填写服务地址和访问令牌，连接后可上传或下载音频。';
  String _playing = '';
  Directory? _root;
  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final values = await Future.wait([
      _vault.read(key: 'boa.storage.address'),
      _vault.read(key: 'boa.storage.token')
    ]);
    final root = await getApplicationDocumentsDirectory();
    _root = Directory('${root.path}/boa_audio');
    await _refreshLocal();
    if (!mounted) return;
    setState(() {
      _address.text = values[0] ?? _address.text;
      _token.text = values[1] ?? '';
    });
  }

  Future<void> _refreshLocal() async {
    final dir = Directory('${_root!.path}/downloads');
    final files = await dir.exists()
        ? await dir
            .list()
            .where((f) => f is File && !f.path.endsWith('.part'))
            .cast<File>()
            .toList()
        : <File>[];
    if (mounted) setState(() => _local = files);
  }

  Future<void> _run(Future<void> Function() work) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _progress = 0;
    });
    try {
      await work();
    } catch (e) {
      if (mounted) {
        setState(() => _message =
            e is StorageException ? e.message : '操作失败，请检查地址、令牌、网络或文件格式');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _update(double v) {
    if (mounted) setState(() => _progress = v);
  }

  Future<void> _connect() => _run(() async {
        _client?.close();
        _client = StorageClient(_address.text.trim(), _token.text.trim());
        final rows = await _client!.list();
        await _vault.write(
            key: 'boa.storage.address', value: _address.text.trim());
        await _vault.write(key: 'boa.storage.token', value: _token.text.trim());
        if (mounted) {
          setState(() {
            _items = rows;
            _message = '已连接 · ${rows.length} 个音频文件';
          });
        }
      });
  Future<void> _upload() => _run(() async {
        final selection = await FilePicker.platform.pickFiles(
            type: FileType.custom,
            allowedExtensions: ['wav', 'm4a', 'mp3', 'ogg', 'flac', 'aac'],
            withData: false);
        final path = selection?.files.single.path;
        if (path == null) return;
        if (mounted) setState(() => _message = '校验并上传，重复选择同一文件可续传');
        await _client!.upload(File(path), Directory('${_root!.path}/sessions'),
            progress: _update);
        final rows = await _client!.list();
        if (mounted) {
          setState(() {
            _items = rows;
            _message = '上传完成，服务端 SHA-256 校验通过';
          });
        }
      });
  Future<void> _download(Map<String, dynamic> row) => _run(() async {
        setState(() => _message = '下载并校验 ${row['filename']}');
        final f = await _client!.download(
            row, Directory('${_root!.path}/downloads'),
            progress: _update);
        await _refreshLocal();
        if (mounted) setState(() => _message = '下载校验通过，准备播放');
        await _play(f, row['filename'] as String);
      });
  Future<void> _play(File f, String title) async {
    await _player.stop();
    await _player.setFilePath(f.path);
    if (mounted) setState(() => _playing = title);
    // play() resolves when playback completes; do not block the operation UI.
    _player.play().catchError((Object e) {
      if (mounted) setState(() => _message = '播放器无法解码此文件');
    });
  }

  @override
  void dispose() {
    _client?.close();
    _player.dispose();
    _address.dispose();
    _token.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        appBar: AppBar(title: const Text('音频存储调试')),
        body: ListView(padding: const EdgeInsets.all(20), children: [
          const Text('HTTP 上传 · 断点续传 · 本地播放',
              style: TextStyle(fontSize: 22, fontWeight: FontWeight.bold)),
          const SizedBox(height: 12),
          const Text('模拟器可用 127.0.0.1；真机请填 Mac 局域网 IP。文件保留在本地，不删除 Clip 原件。'),
          TextField(
              controller: _address,
              enabled: !_busy,
              decoration: const InputDecoration(labelText: '服务地址')),
          TextField(
              controller: _token,
              enabled: !_busy,
              obscureText: true,
              decoration: const InputDecoration(labelText: '访问令牌（安全存储）')),
          const SizedBox(height: 16),
          Wrap(spacing: 12, runSpacing: 8, children: [
            FilledButton(
                onPressed: _busy ? null : _connect,
                child: const Text('连接 / 刷新')),
            OutlinedButton(
                onPressed:
                    !_busy && _client != null && _root != null ? _upload : null,
                child: const Text('选择音频上传')),
            if (_busy)
              TextButton(
                  onPressed: () {
                    _client?.close();
                    setState(() => _message = '已取消，请重新连接后续传');
                  },
                  child: const Text('取消')),
          ]),
          if (_busy)
            LinearProgressIndicator(value: _progress == 0 ? null : _progress),
          Padding(
              padding: const EdgeInsets.symmetric(vertical: 16),
              child: Text(_message)),
          if (_playing.isNotEmpty) ...[
            Text('正在播放：$_playing'),
            StreamBuilder<PlayerState>(
                stream: _player.playerStateStream,
                builder: (_, s) => IconButton(
                      icon: Icon(s.data?.playing == true
                          ? Icons.pause_circle
                          : Icons.play_circle),
                      onPressed: () async {
                        if (_player.playing) {
                          await _player.pause();
                        } else {
                          if (_player.processingState ==
                              ProcessingState.completed) {
                            await _player.seek(Duration.zero);
                          }
                          _player.play();
                        }
                      },
                    )),
            StreamBuilder<Duration>(
                stream: _player.positionStream,
                builder: (_, s) {
                  final length =
                      (_player.duration?.inMilliseconds ?? 0).toDouble();
                  final value = (s.data?.inMilliseconds ?? 0)
                      .toDouble()
                      .clamp(0.0, length);
                  return Column(children: [
                    Slider(
                        value: value,
                        max: length > 0 ? length : 1,
                        onChanged: length == 0
                            ? null
                            : (v) => _player
                                .seek(Duration(milliseconds: v.toInt()))),
                    Text(
                        '${(value / 1000).toStringAsFixed(1)} / ${(length / 1000).toStringAsFixed(1)} 秒')
                  ]);
                }),
          ],
          const Divider(),
          const Text('服务器音频',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          for (final row in _items)
            ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(row['filename'] as String),
                subtitle: Text(
                    '${((row['size'] as int) / 1024 / 1024).toStringAsFixed(2)} MiB'),
                trailing: IconButton(
                    icon: const Icon(Icons.download),
                    onPressed: _busy ? null : () => _download(row))),
          const Divider(),
          const Text('已校验的本地音频',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold)),
          for (final f in _local)
            ListTile(
                contentPadding: EdgeInsets.zero,
                title: Text(f.uri.pathSegments.last),
                trailing: IconButton(
                    icon: const Icon(Icons.play_arrow),
                    onPressed: _busy
                        ? null
                        : () => _run(() => _play(f, f.uri.pathSegments.last)))),
        ]),
      );
}
