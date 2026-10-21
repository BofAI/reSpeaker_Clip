import 'dart:async';
import 'dart:convert';
import 'package:flutter/material.dart';
import 'package:sensecraft_voice/sensecraft_voice.dart';
import 'package:shared_preferences/shared_preferences.dart';
import 'clip_protocol.dart';

class DesktopClipPage extends StatefulWidget {
  const DesktopClipPage({super.key});
  @override
  State<DesktopClipPage> createState() => _DesktopClipPageState();
}

class _DesktopClipPageState extends State<DesktopClipPage> {
  final _sdk = SenseCraftVoiceClient();
  final List<StreamSubscription<dynamic>> _subscriptions = [];
  StreamSubscription<BluetoothConnectionState>? _link;
  StreamSubscription<int>? _batterySub;
  SenseCraftVoiceConnection? _connection;
  AtTransport? _at;
  List<ScanResult> _results = [];
  bool _busy = false, _scanning = false, _listening = false;
  bool _filterService = false;
  String _savedId = '', _savedName = '';
  String _adapter = '点击搜索后检查蓝牙';
  String _message = '打开 Clip 并靠近 Mac。如果手机正在连接设备，请先在手机端断开。';
  String _details = '';
  int? _battery;

  @override
  void initState() {
    super.initState();
    _restore();
  }

  Future<void> _restore() async {
    final prefs = await SharedPreferences.getInstance();
    if (!mounted) return;
    setState(() {
      _savedId = prefs.getString('boa.clip.id') ?? '';
      _savedName = prefs.getString('boa.clip.name') ?? '';
      _filterService = prefs.getBool('boa.clip.filterService') ?? false;
    });
  }

  void _listen() {
    if (_listening) return;
    _listening = true;
    _subscriptions.add(_sdk.scanResults.listen((rows) {
      if (mounted) setState(() => _results = rows);
    }, onError: (Object _) {
      if (mounted) setState(() => _message = '扫描失败，请检查系统蓝牙权限');
    }));
    _subscriptions.add(_sdk.isScanning.listen((v) {
      if (mounted) setState(() => _scanning = v);
    }));
    _subscriptions.add(_sdk.adapterState.listen((v) {
      if (mounted) {
        setState(() => _adapter = switch (v) {
              BluetoothAdapterState.on => '蓝牙已开启',
              BluetoothAdapterState.off => '蓝牙已关闭，请在系统设置中开启',
              BluetoothAdapterState.unauthorized =>
                '未授权，请在系统设置 → 隐私与安全性 → 蓝牙中允许 Boa Voice',
              BluetoothAdapterState.unavailable => '当前系统不支持蓝牙',
              _ => '蓝牙状态：${v.name}',
            });
      }
    }, onError: (Object _) {
      if (mounted) setState(() => _adapter = '无法获取蓝牙状态');
    }));
  }

  Future<void> _run(Future<void> Function() action) async {
    if (_busy) return;
    setState(() => _busy = true);
    try {
      await action();
    } catch (e) {
      if (mounted) {
        setState(() => _message = '操作失败：$e。请检查权限、距离和其他设备连接；写命令不会自动重试。');
      }
    } finally {
      if (mounted) setState(() => _busy = false);
    }
  }

  Future<void> _scan() => _run(() async {
        _listen();
        setState(() {
          _results = [];
          _message = '搜索附近 Clip（约 12 秒）…';
        });
        await _sdk.startScan(filterByService: _filterService);
      });
  Future<void> _connect(ScanResult? result) => _run(() async {
        _listen();
        await _sdk.stopScan();
        await _link?.cancel();
        await _batterySub?.cancel();
        final candidate = result == null
            ? await _sdk.connectByDeviceId(_savedId)
            : await _sdk.connect(result);
        if (candidate == null) throw StateError('无法连接已记住的设备，请重新搜索');
        if (!mounted) {
          await _sdk.disconnect(candidate);
          return;
        }
        _connection = candidate;
        _at = candidate.createAtTransport();
        _savedId = candidate.device.remoteId.toString();
        final name =
            result?.advertisementData.advName ?? candidate.device.platformName;
        _savedName = name.isEmpty ? 'reSpeaker Clip' : name;
        _link = candidate.device.connectionState.listen((state) {
          if (state == BluetoothConnectionState.disconnected && mounted) {
            setState(() {
              _connection = null;
              _at = null;
              _battery = null;
              _details = '';
              _message = 'Clip 已断开；设备可能仍在录音，重连后请刷新状态。';
            });
          }
        });
        _batterySub = candidate.batteryLevelStream?.listen((v) {
          if (mounted) setState(() => _battery = v);
        });
        final prefs = await SharedPreferences.getInstance();
        await prefs.setString('boa.clip.id', _savedId);
        await prefs.setString('boa.clip.name', _savedName);
        if (mounted) setState(() => _message = '蓝牙已连接 · $_savedName');
        await _query();
      });
  Future<void> _query() async {
    final at = _at;
    if (at == null) return;
    final replies = <String, dynamic>{};
    for (final command in ['AT+VERSION', 'AT+GSTAT']) {
      try {
        replies[command] = checkedClipReply(await at.send(command));
      } catch (e) {
        replies[command] = {'error': e.toString()};
      }
    }
    if (mounted) {
      setState(
          () => _details = const JsonEncoder.withIndent('  ').convert(replies));
    }
  }

  Future<void> _command(String label, String command) => _run(() async {
        final at = _at;
        if (at == null) return;
        checkedClipReply(await at.send(command));
        if (mounted) setState(() => _message = '$label：设备已确认。');
        await _query();
      });
  Future<void> _disconnect() => _run(() async {
        final connection = _connection;
        if (connection != null) await _sdk.disconnect(connection);
        await _link?.cancel();
        await _batterySub?.cancel();
        if (mounted) {
          setState(() {
            _connection = null;
            _at = null;
            _battery = null;
            _details = '';
            _message = '已断开；不会停止设备独立录音。';
          });
        }
      });
  @override
  void dispose() {
    for (final sub in _subscriptions) {
      unawaited(sub.cancel());
    }
    _link?.cancel();
    _batterySub?.cancel();
    if (_listening) unawaited(_sdk.stopScan().catchError((Object _) {}));
    final conn = _connection;
    if (conn != null) {
      unawaited(_sdk.disconnect(conn).catchError((Object _) {}));
    }
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListView(children: [
        Card(
            child: Padding(
                padding: const EdgeInsets.all(20),
                child: Column(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Row(children: [
                        const Icon(Icons.bluetooth, color: Color(0xff167d8d)),
                        const SizedBox(width: 12),
                        Expanded(
                            child: Text(
                                _connection == null
                                    ? '连接你的 reSpeaker Clip'
                                    : _savedName,
                                style: Theme.of(context).textTheme.titleLarge)),
                        if (_battery != null) Text('电量 $_battery%'),
                      ]),
                      const SizedBox(height: 12),
                      Text(_adapter),
                      const SizedBox(height: 12),
                      Text(_message),
                      const SizedBox(height: 16),
                      Wrap(spacing: 12, runSpacing: 8, children: [
                        FilledButton.icon(
                            onPressed:
                                _busy || _connection != null ? null : _scan,
                            icon: const Icon(Icons.search),
                            label: Text(_scanning ? '重新搜索' : '搜索 Clip')),
                        if (_scanning)
                          OutlinedButton(
                              onPressed: () => _run(_sdk.stopScan),
                              child: const Text('停止搜索')),
                        if (_connection != null) ...[
                          OutlinedButton(
                              onPressed: _busy ? null : () => _run(_query),
                              child: const Text('刷新设备状态')),
                          OutlinedButton(
                              onPressed: _busy ? null : _disconnect,
                              child: const Text('断开连接')),
                        ],
                      ]),
                      if (_busy || _scanning)
                        const Padding(
                            padding: EdgeInsets.only(top: 16),
                            child: LinearProgressIndicator()),
                    ]))),
        if (_connection == null && _savedId.isNotEmpty)
          ListTile(
              title: Text('已记住：$_savedName'),
              subtitle: Text(_savedId),
              trailing: Wrap(children: [
                TextButton(
                    onPressed: _busy ? null : () => _connect(null),
                    child: const Text('重新连接')),
                TextButton(
                    onPressed: _busy
                        ? null
                        : () => _run(() async {
                              final prefs =
                                  await SharedPreferences.getInstance();
                              await prefs.remove('boa.clip.id');
                              await prefs.remove('boa.clip.name');
                              if (mounted) {
                                setState(() {
                                  _savedId = '';
                                  _savedName = '';
                                });
                              }
                            }),
                    child: const Text('忘记记录'))
              ])),
        if (_connection == null) ...[
          SwitchListTile(
              title: const Text('按设备服务 UUID 搜索'),
              subtitle: const Text('默认按 Clip 名称搜索；只有固件广播服务 UUID 时才开启此项。'),
              value: _filterService,
              onChanged: _busy || _scanning
                  ? null
                  : (v) => _run(() async {
                        final prefs = await SharedPreferences.getInstance();
                        await prefs.setBool('boa.clip.filterService', v);
                        if (mounted) setState(() => _filterService = v);
                      })),
          for (final result in _results)
            ListTile(
                leading: const Icon(Icons.sensors),
                title: Text(result.advertisementData.advName.isEmpty
                    ? result.device.platformName
                    : result.advertisementData.advName),
                subtitle:
                    Text('${result.device.remoteId} · 信号 ${result.rssi} dBm'),
                trailing: FilledButton(
                    onPressed: _busy ? null : () => _connect(result),
                    child: const Text('连接'))),
          if (!_scanning && _results.isEmpty)
            const Padding(
                padding: EdgeInsets.all(16),
                child: Text('没有扫描结果时，检查 Clip 是否开机、蓝牙权限是否允许，并断开手机上的设备连接。')),
        ],
        if (_connection != null) ...[
          const Padding(
              padding: EdgeInsets.symmetric(vertical: 16),
              child: Text('Clip 录音控制',
                  style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold))),
          Wrap(spacing: 8, runSpacing: 8, children: [
            for (final entry in clipRecordingCommands.entries)
              OutlinedButton(
                  onPressed:
                      _busy ? null : () => _command(entry.key, entry.value),
                  child: Text(entry.key))
          ]),
          const SizedBox(height: 16),
          SelectableText(_details),
        ],
        const SizedBox(height: 24),
        const Text('连接说明', style: TextStyle(fontWeight: FontWeight.bold)),
        const SizedBox(height: 8),
        const Text(
            '首次搜索需允许 macOS 蓝牙权限。记住设备只保存本机连接记录，不代表账号绑定；忘记记录不会恢复设备出厂设置。\n设备录音控制与本机麦克风录音相互独立。当前桌面入口支持 BLE 连接和控制，Clip 文件下载、Wi-Fi 同步及 OTA 尚未接入；不会自动删除设备录音。'),
      ]);
}
