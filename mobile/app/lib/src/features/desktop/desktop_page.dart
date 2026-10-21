import 'dart:io';
import 'dart:ui' show AppExitResponse;
import 'dart:async';
import 'dart:convert';
import 'package:record/record.dart';
import 'backup_queue.dart';
import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'server_config.dart';
import 'package:just_audio/just_audio.dart';
import 'package:path_provider/path_provider.dart';
import '../audio_storage/storage_client.dart';
import 'local_library.dart';
import 'audio_presence.dart';
import 'desktop_clip_page.dart';

class DesktopPage extends StatefulWidget {
  const DesktopPage({super.key});
  @override
  State<DesktopPage> createState() => _DesktopPageState();
}

class _DesktopPageState extends State<DesktopPage> {
  final _address = TextEditingController(text: 'http://127.0.0.1:8765');
  final _token = TextEditingController();
  final _search = TextEditingController();
  final _player = AudioPlayer();
  ServerConfig? _serverConfig;
  final _recorder = AudioRecorder();
  final _clock = Stopwatch();
  Timer? _timer;
  BackupQueue? _queue;
  bool _autoBackup = true;
  bool _recording = false;
  bool _paused = false;
  bool _recordAction = false;
  String? _recordPath;
  String _endpoint = '';
  String _connectedToken = '';
  late final AppLifecycleListener _lifecycle;
  LocalLibrary? _library;
  Directory? _root;
  StorageClient? _client;
  List<LocalAudio> _local = [];
  List<Map<String, dynamic>> _remote = [];
  int _tab = 0;
  bool _clipOpened = false;
  bool _busy = false;
  double? _progress;
  String _status = '本地音频随时可听，连接服务后可以备份和下载。';
  String _playing = '';

  @override
  void initState() {
    super.initState();
    _lifecycle = AppLifecycleListener(onExitRequested: () async {
      if (_recording || _recordAction) {
        if (mounted) setState(() => _status = '请先停止并保存录音，再退出应用。');
        return AppExitResponse.cancel;
      }
      return AppExitResponse.exit;
    });
    _initialize();
    _timer = Timer.periodic(const Duration(seconds: 1), (_) {
      if (_recording && mounted) setState(() {});
      if (DateTime.now().second % 10 == 0) unawaited(_pump());
    });
  }

  Future<void> _initialize() async {
    await _work(() async {
      final dir = await getApplicationSupportDirectory();
      _root = Directory('${dir.path}/boa_desktop');
      _library = LocalLibrary(Directory('${_root!.path}/library'));
      _local = await _library!.list();
      _queue = BackupQueue(File('${_root!.path}/backup-queue.json'));
      await _queue!.load();
      final settings = File('${_root!.path}/preferences.json');
      if (await settings.exists()) {
        _autoBackup =
            (jsonDecode(await settings.readAsString()) as Map)['autoBackup'] ==
                true;
      }
      _serverConfig =
          ServerConfig(File('${_root!.path}/credentials/server.json'));
      try {
        final config = await _serverConfig!.read();
        _address.text = config['address'] ?? _address.text;
        _token.text = config['token'] ?? '';
      } catch (_) {
        _status = '本地服务配置读取失败，请重新填写；音频文件不受影响。';
      }
    });
    if (_token.text.isNotEmpty) {
      _endpoint = _address.text.trim();
      _connectedToken = _token.text.trim();
      await _enqueueAll();
      await _connect();
    }
  }

  Future<void> _work(Future<void> Function() action) async {
    if (_busy) return;
    setState(() {
      _busy = true;
      _progress = null;
    });
    try {
      await action();
    } catch (e) {
      _status = e is StorageException
          ? e.message
          : e is FormatException
              ? e.message
              : '操作未完成，请检查网络、文件权限或音频格式。';
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  void _update(double value) {
    if (mounted) setState(() => _progress = value);
  }

  Future<void> _connect() => _work(() async {
        _client?.close();
        _client = null;
        final candidate =
            StorageClient(_address.text.trim(), _token.text.trim());
        try {
          final rows = await candidate.list();
          _client = candidate;
          _remote = rows;
          _endpoint = _address.text.trim();
          _connectedToken = _token.text.trim();
        } catch (_) {
          candidate.close();
          rethrow;
        }
        await _enqueueAll();
        _status = '已连接服务器 · 自动备份${_autoBackup ? '已开启' : '已关闭'}';
        try {
          await _serverConfig!.save(_address.text.trim(), _token.text.trim());
        } catch (_) {
          _status = '已连接；本地配置保存失败，本次配置仅在内存中使用。';
        }
      });

  Future<void> _import() => _work(() async {
        final picked = await FilePicker.platform.pickFiles(
          type: FileType.custom,
          allowedExtensions: ['wav', 'm4a', 'mp3', 'ogg', 'flac', 'aac'],
          withData: false,
        );
        final path = picked?.files.single.path;
        if (path == null) return;
        await _library!.importFile(File(path));
        _local = await _library!.list();
        _tab = 0;
        _status = '导入完成 · 已保存独立副本，原文件保留';
        await _enqueueAll();
      });

  Future<void> _enqueueAll() async {
    if (!_autoBackup || _endpoint.isEmpty || _queue == null) return;
    for (final audio in _local) {
      final hash = audio.file.uri.pathSegments.last.split('.').first;
      if (_remote
          .any((row) => row['sha256'] == hash && row['size'] == audio.bytes)) {
        await _queue!.markBackedUp(audio, _endpoint);
      } else {
        await _queue!.enqueue(audio, _endpoint);
      }
    }
    unawaited(_pump());
  }

  Future<void> _pump() async {
    if (!_autoBackup ||
        _connectedToken.isEmpty ||
        _queue == null ||
        _queue!.running ||
        !mounted) {
      return;
    }
    final endpoint = _endpoint;
    final token = _connectedToken;
    try {
      await _queue!.drain(
          (audio, destination) async {
            if (destination != endpoint) {
              throw const FormatException('请连接原备份服务器');
            }
            final client = StorageClient(destination, token);
            try {
              final uploaded = await client.upload(
                  audio.file, Directory('${_root!.path}/sessions'),
                  filename: audio.name);
              if (mounted && _endpoint == destination) {
                setState(() {
                  _remote.removeWhere((r) => r['id'] == uploaded['id']);
                  _remote.add(uploaded);
                });
              }
            } finally {
              client.close();
            }
          },
          endpoint: endpoint,
          allowed: () => mounted && _autoBackup && _endpoint == endpoint,
          changed: () {
            if (mounted) setState(() {});
          });
    } catch (_) {
      if (mounted) setState(() => _status = '备份队列无法保存，请检查磁盘空间；原文件保留。');
    }
  }

  Future<void> _upload(LocalAudio audio) async {
    if (_queue == null || _endpoint.isEmpty) return;
    await _queue!.enqueue(audio, _endpoint);
    await _queue!.retryFailed();
    await _pump();
  }

  Future<void> _toggleBackup(bool enabled) async {
    setState(() => _autoBackup = enabled);
    await _work(() async {
      final settings = File('${_root!.path}/preferences.json');
      await settings.parent.create(recursive: true);
      await settings.writeAsString(jsonEncode({'autoBackup': enabled}),
          flush: true);
      await _enqueueAll();
      _status = enabled ? '自动备份已开启，包含现有本地音频' : '自动备份已关闭；当前已开始的上传会完成';
    });
  }

  Future<void> _startRecording() async {
    if (_recordAction || _recording || _root == null) return;
    setState(() => _recordAction = true);
    try {
      if (!await _recorder.hasPermission()) {
        throw const FormatException('请在系统设置 → 隐私与安全性 → 麦克风中允许 Boa Voice');
      }
      await _player.stop();
      final dir = Directory('${_root!.path}/recordings');
      await dir.create(recursive: true);
      _recordPath =
          '${dir.path}/录音-${DateTime.now().millisecondsSinceEpoch}.wav';
      await _recorder.start(
          const RecordConfig(
              encoder: AudioEncoder.wav, sampleRate: 44100, numChannels: 1),
          path: _recordPath!);
      _clock.reset();
      _clock.start();
      _recording = true;
      _paused = false;
      _status = '麦克风录音中 · 停止后自动保存并排队备份';
    } catch (e) {
      _status = e is FormatException ? e.message : '无法启动麦克风，请检查系统权限和输入设备';
    } finally {
      if (mounted) setState(() => _recordAction = false);
    }
  }

  Future<void> _pauseRecording() async {
    if (_recordAction) return;
    setState(() => _recordAction = true);
    try {
      if (_paused) {
        await _recorder.resume();
        _clock.start();
      } else {
        await _recorder.pause();
        _clock.stop();
      }
      _paused = !_paused;
    } catch (_) {
      _status = '暂停/继续失败，请重试或停止保存';
    } finally {
      if (mounted) setState(() => _recordAction = false);
    }
  }

  Future<void> _stopRecording() async {
    if (_recordAction) return;
    setState(() => _recordAction = true);
    try {
      final path = await _recorder.stop() ?? _recordPath;
      _clock.stop();
      _recording = false;
      _paused = false;
      if (path == null || await File(path).length() <= 44) {
        throw const FormatException('没有收到有效音频，请检查麦克风输入');
      }
      final source = File(path);
      await _library!.importFile(source);
      _local = await _library!.list();
      // Delete only the staging recording after its verified library copy exists.
      await source.delete();
      _recordPath = null;
      _tab = 0;
      _status = '录音已保存到本地音频库';
      await _enqueueAll();
    } catch (e) {
      _status = e is FormatException ? e.message : '保存失败，录音暂存文件已保留，请检查磁盘空间';
    } finally {
      if (mounted) setState(() => _recordAction = false);
    }
  }

  Future<void> _download(Map<String, dynamic> row) => _work(() async {
        _status = '正在下载 ${row['filename']}';
        final downloaded = await _client!.download(
            row, Directory('${_root!.path}/downloads'),
            progress: _update);
        final imported = await _library!
            .importFile(downloaded, name: row['filename'] as String);
        await _queue?.markBackedUp(imported, _endpoint);
        _local = await _library!.list();
        // Only the duplicate staging copy is removed after verified local import.
        await downloaded.delete();
        _status = '下载完成 · 已保存到本地音频库';
      });

  Future<void> _play(LocalAudio audio) => _work(() async {
        await _player.stop();
        await _player.setFilePath(audio.file.path);
        _playing = audio.name;
        _status = '正在播放 ${audio.name}';
        _startPlayback();
      });

  void _startPlayback() {
    _player.play().catchError((Object _) {
      if (mounted) setState(() => _status = '播放器无法读取此音频，请检查格式。');
    });
  }

  @override
  void dispose() {
    _timer?.cancel();
    _lifecycle.dispose();
    _recorder.dispose();
    _client?.close();
    _player.dispose();
    _address.dispose();
    _token.dispose();
    _search.dispose();
    super.dispose();
  }

  String _size(int bytes) => '${(bytes / 1024 / 1024).toStringAsFixed(1)} MB';
  String _time(Duration value) {
    final seconds = value.inSeconds;
    return '${seconds ~/ 60}:${(seconds % 60).toString().padLeft(2, '0')}';
  }

  Widget _empty(IconData icon, String title, String detail) => Center(
          child: Column(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(icon, size: 56, color: const Color(0xff167d8d)),
          const SizedBox(height: 18),
          Text(title, style: Theme.of(context).textTheme.titleLarge),
          const SizedBox(height: 8),
          Text(detail, textAlign: TextAlign.center),
        ],
      ));

  Widget _files() {
    final query = _search.text.toLowerCase();
    if (_tab == 0) {
      final rows =
          _local.where((r) => r.name.toLowerCase().contains(query)).toList();
      if (rows.isEmpty) {
        return _empty(
            Icons.library_music_outlined,
            query.isEmpty ? '你的音频工作台' : '没有找到匹配音频',
            query.isEmpty ? '点击“导入音频”，开始在 Mac 上整理和回听。' : '换个关键词试试。');
      }
      return ListView.separated(
          itemCount: rows.length,
          separatorBuilder: (_, __) => const SizedBox(height: 8),
          itemBuilder: (_, i) {
            final row = rows[i];
            return Card(
                elevation: 0,
                color: Colors.white,
                child: ListTile(
                  leading: const CircleAvatar(
                      child: Icon(Icons.audio_file_outlined)),
                  title: Text(row.name,
                      maxLines: 1, overflow: TextOverflow.ellipsis),
                  subtitle: Text(
                      '${localPresence(row, _remote, connected: _client != null)} · ${_size(row.bytes)} · 入库 ${audioTime(row.addedAt)}\n ${_endpoint.isEmpty ? '待配置备份' : _queue?.status(row, _endpoint) ?? '待备份'}'),
                  onTap: _busy || _recording ? null : () => _play(row),
                  trailing: Row(mainAxisSize: MainAxisSize.min, children: [
                    IconButton(
                        tooltip: '播放',
                        onPressed:
                            _busy || _recording ? null : () => _play(row),
                        icon: const Icon(Icons.play_arrow)),
                    IconButton(
                        tooltip: !_autoBackup ? '请开启自动备份' : '重试备份',
                        onPressed: _busy || _client == null || !_autoBackup
                            ? null
                            : () => _upload(row),
                        icon: const Icon(Icons.cloud_upload_outlined)),
                  ]),
                ));
          });
    }
    final rows = _remote
        .where((r) => (r['filename'] as String).toLowerCase().contains(query))
        .toList()
      ..sort((a, b) => remoteTime(b).compareTo(remoteTime(a)));
    if (_client == null) {
      return _empty(Icons.cloud_outlined, '连接你的音频服务器', '在“服务设置”填写地址和令牌，然后连接。');
    }
    if (rows.isEmpty) {
      return _empty(
          Icons.cloud_done_outlined, '暂无匹配的服务器音频', '从本地音频库上传，或清除搜索条件。');
    }
    return ListView.builder(
        itemCount: rows.length,
        itemBuilder: (_, i) {
          final row = rows[i];
          return Card(
              elevation: 0,
              color: Colors.white,
              child: ListTile(
                leading: const Icon(Icons.cloud_done_outlined),
                title: Text(row['filename'] as String,
                    maxLines: 1, overflow: TextOverflow.ellipsis),
                subtitle: Text(
                    '${remotePresence(row, _local)} · ${_size(row['size'] as int)}\n上传 ${audioTime(remoteTime(row))}'),
                trailing: IconButton(
                    tooltip: '下载到本地',
                    onPressed: _busy ? null : () => _download(row),
                    icon: const Icon(Icons.download_outlined)),
              ));
        });
  }

  Widget _settings() => ListView(children: [
        Text('连接自有存储', style: Theme.of(context).textTheme.titleLarge),
        const SizedBox(height: 12),
        const Text('本机服务使用 127.0.0.1；其他电脑使用服务器地址。\n音频保存在应用本地目录，关闭服务仍可播放。'),
        const SizedBox(height: 16),
        SwitchListTile(
            title: const Text('自动上传备份'),
            subtitle: const Text('连接后自动上传现有音频、新录音和导入文件；原件保留。应用退出时暂停，下次打开继续。'),
            value: _autoBackup,
            onChanged: _busy || _root == null ? null : _toggleBackup),
        const SizedBox(height: 24),
        TextField(
            controller: _address,
            enabled: !_busy && !(_queue?.running ?? false),
            decoration: const InputDecoration(labelText: '服务地址')),
        const SizedBox(height: 16),
        TextField(
            controller: _token,
            enabled: !_busy && !(_queue?.running ?? false),
            obscureText: true,
            decoration: const InputDecoration(
                labelText: '访问令牌', helperText: '保存在 App 沙箱文件中，不加密，仅当前用户可读')),
        const SizedBox(height: 20),
        Align(
            alignment: Alignment.centerLeft,
            child: FilledButton.icon(
                onPressed:
                    _busy || (_queue?.running ?? false) ? null : _connect,
                icon: const Icon(Icons.link),
                label: const Text('连接并保存'))),
        const SizedBox(height: 32),
        const Text('当前版本', style: TextStyle(fontWeight: FontWeight.bold)),
        const SizedBox(height: 8),
        const Text(
            'Mac 音频工作台 · Beta 1.0.1-beta.1\n支持本地导入、播放、HTTP 备份与下载。\n支持麦克风录音和自动备份；Clip 蓝牙连接及控制见“Clip 设备”；模型处理尚未接入。'),
      ]);

  Widget _playbar() => Container(
        padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
        decoration: const BoxDecoration(
            color: Colors.white,
            border: Border(top: BorderSide(color: Color(0xffe4e7ec)))),
        child: Row(children: [
          StreamBuilder<PlayerState>(
              stream: _player.playerStateStream,
              builder: (_, snapshot) => IconButton.filled(
                  tooltip: snapshot.data?.playing == true ? '暂停' : '播放',
                  onPressed: _playing.isEmpty || _recording
                      ? null
                      : () async {
                          if (_player.playing) {
                            await _player.pause();
                          } else {
                            if (_player.processingState ==
                                ProcessingState.completed) {
                              await _player.seek(Duration.zero);
                            }
                            _startPlayback();
                          }
                        },
                  icon: Icon(snapshot.data?.playing == true
                      ? Icons.pause
                      : Icons.play_arrow))),
          const SizedBox(width: 16),
          SizedBox(
              width: 180,
              child: Text(_playing.isEmpty ? '选择一段音频开始播放' : _playing,
                  maxLines: 2, overflow: TextOverflow.ellipsis)),
          const SizedBox(width: 16),
          Expanded(
              child: StreamBuilder<Duration>(
                  stream: _player.positionStream,
                  builder: (_, snapshot) {
                    final duration = _player.duration ?? Duration.zero;
                    final position = snapshot.data ?? Duration.zero;
                    final max = duration.inMilliseconds.toDouble();
                    return Row(children: [
                      Text(_time(position)),
                      Expanded(
                          child: Slider(
                        value: position.inMilliseconds
                            .toDouble()
                            .clamp(0, max > 0 ? max : 1),
                        max: max > 0 ? max : 1,
                        onChanged: max == 0
                            ? null
                            : (v) =>
                                _player.seek(Duration(milliseconds: v.toInt())),
                      )),
                      Text(_time(duration))
                    ]);
                  })),
        ]),
      );

  @override
  Widget build(BuildContext context) => Scaffold(
          body: Column(children: [
        Expanded(
            child: Row(children: [
          Container(
              width: 200,
              color: const Color(0xffedf2f3),
              child: Column(children: [
                const Padding(
                    padding: EdgeInsets.fromLTRB(20, 30, 20, 28),
                    child: Row(children: [
                      Icon(Icons.graphic_eq, color: Color(0xff167d8d)),
                      SizedBox(width: 8),
                      Text('Boa Voice',
                          style: TextStyle(
                              fontSize: 21, fontWeight: FontWeight.w700)),
                    ])),
                for (final item in [
                  (Icons.library_music_outlined, '本地音频'),
                  (Icons.cloud_outlined, '服务器音频'),
                  (Icons.tune, '服务设置'),
                  (Icons.bluetooth, 'Clip 设备')
                ].asMap().entries)
                  Padding(
                      padding: const EdgeInsets.symmetric(
                          horizontal: 12, vertical: 4),
                      child: ListTile(
                        selected: _tab == item.key,
                        selectedTileColor: const Color(0xffd6e9e9),
                        shape: RoundedRectangleBorder(
                            borderRadius: BorderRadius.circular(12)),
                        leading: Icon(item.value.$1),
                        title: Text(item.value.$2),
                        onTap: () => setState(() {
                          _tab = item.key;
                          if (_tab == 3) _clipOpened = true;
                          _search.clear();
                        }),
                      )),
                const Spacer(),
                const Padding(
                    padding: EdgeInsets.all(20),
                    child: Text('个人音频工作台\nmacOS · 本地优先',
                        style: TextStyle(color: Color(0xff667085)))),
              ])),
          Expanded(
              child: Padding(
                  padding: const EdgeInsets.all(28),
                  child: Column(
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Row(children: [
                          Expanded(
                              child: Text(
                                  ['本地音频', '服务器音频', '服务设置', 'Clip 设备'][_tab],
                                  style: const TextStyle(
                                      fontSize: 28,
                                      fontWeight: FontWeight.bold))),
                          if (_tab == 1)
                            IconButton(
                                tooltip: '刷新服务器',
                                onPressed: _busy ||
                                        _client == null ||
                                        (_queue?.running ?? false)
                                    ? null
                                    : _connect,
                                icon: const Icon(Icons.refresh)),
                          FilledButton.icon(
                              onPressed:
                                  _recordAction || _root == null || _recording
                                      ? null
                                      : _startRecording,
                              icon: const Icon(Icons.mic),
                              label: const Text('麦克风录音')),
                          const SizedBox(width: 12),
                          FilledButton.icon(
                              onPressed:
                                  _busy || _library == null ? null : _import,
                              icon: const Icon(Icons.add),
                              label: const Text('导入音频')),
                        ]),
                        const SizedBox(height: 20),
                        if (_tab < 2) ...[
                          TextField(
                              controller: _search,
                              onChanged: (_) => setState(() {}),
                              decoration: const InputDecoration(
                                  prefixIcon: Icon(Icons.search),
                                  hintText: '搜索音频名称 · 时间从新到旧')),
                          const SizedBox(height: 20),
                        ],
                        if (_recording)
                          Card(
                              color: const Color(0xffffe9e8),
                              child: Padding(
                                  padding: const EdgeInsets.all(12),
                                  child: Row(children: [
                                    const Icon(Icons.mic, color: Colors.red),
                                    const SizedBox(width: 12),
                                    Expanded(
                                        child: Text(
                                            '${_paused ? '已暂停' : '正在录音'}  ${_time(_clock.elapsed)}')),
                                    TextButton(
                                        onPressed: _recordAction
                                            ? null
                                            : _pauseRecording,
                                        child: Text(_paused ? '继续' : '暂停')),
                                    FilledButton(
                                        onPressed: _recordAction
                                            ? null
                                            : _stopRecording,
                                        child: const Text('停止并保存')),
                                  ]))),
                        Expanded(
                            child: IndexedStack(
                                index: _tab == 3 ? 1 : 0,
                                children: [
                              _tab == 2 ? _settings() : _files(),
                              _clipOpened
                                  ? const DesktopClipPage()
                                  : const SizedBox.shrink(),
                            ])),
                        const SizedBox(height: 16),
                        if (_busy) LinearProgressIndicator(value: _progress),
                        const SizedBox(height: 8),
                        Text(_status,
                            maxLines: 2,
                            overflow: TextOverflow.ellipsis,
                            style: const TextStyle(color: Color(0xff667085))),
                      ]))),
        ])),
        _playbar(),
      ]));
}
