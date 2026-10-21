import 'dart:convert';
import 'dart:io';
import 'dart:math';
import 'package:crypto/crypto.dart';

class StorageException implements Exception {
  final String message;
  final int? status;
  StorageException(this.message, [this.status]);
  @override
  String toString() => message;
}

class StorageClient {
  final Uri base;
  final String token;
  final HttpClient _http = HttpClient()
    ..connectionTimeout = const Duration(seconds: 15);
  bool _closed = false;
  StorageClient(String address, this.token) : base = Uri.parse(address) {
    if (!['http', 'https'].contains(base.scheme) ||
        base.host.isEmpty ||
        base.userInfo.isNotEmpty ||
        base.hasQuery ||
        base.hasFragment ||
        (base.path.isNotEmpty && base.path != '/')) {
      throw ArgumentError('请输入服务根地址，如 http://127.0.0.1:8765');
    }
    if (token.trim().isEmpty || token.contains(RegExp(r'[\r\n]'))) {
      throw ArgumentError('访问令牌不能为空');
    }
  }
  void close() {
    _closed = true;
    _http.close(force: true);
  }

  void _check() {
    if (_closed) throw StorageException('操作已取消，可稍后续传');
  }

  Future<HttpClientRequest> _request(String method, String path) async {
    _check();
    final r = await _http.openUrl(method, base.resolve(path));
    r.followRedirects = false;
    r.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
    return r;
  }

  Future<Map<String, dynamic>> json(String method, String path,
      {Map<String, dynamic>? body}) async {
    final r = await _request(method, path);
    if (body != null) {
      r.headers.contentType = ContentType.json;
      r.write(jsonEncode(body));
    }
    final response = await r.close().timeout(const Duration(seconds: 120));
    if (response.statusCode < 200 || response.statusCode >= 300) {
      await response.drain<void>();
      throw StorageException(
          '服务请求失败（HTTP ${response.statusCode}）', response.statusCode);
    }
    final bytes = <int>[];
    await for (final b in response.timeout(const Duration(seconds: 30))) {
      if (bytes.length + b.length > 4 * 1024 * 1024) {
        throw StorageException('服务响应过大');
      }
      bytes.addAll(b);
    }
    return jsonDecode(utf8.decode(bytes)) as Map<String, dynamic>;
  }

  Future<List<Map<String, dynamic>>> list() async {
    final rows = <Map<String, dynamic>>[];
    for (var offset = 0;; offset += 100) {
      final data =
          await json('GET', '/api/audio/recordings?offset=$offset&limit=100');
      final page = (data['items'] as List).cast<Map<String, dynamic>>();
      rows.addAll(page);
      if (page.length < 100) return rows;
    }
  }

  static Future<String> hash(File f) async =>
      (await sha256.bind(f.openRead()).first).toString();
  static String mimeFor(String path) {
    switch (path.split('.').last.toLowerCase()) {
      case 'wav':
        return 'audio/wav';
      case 'm4a':
        return 'audio/mp4';
      case 'mp3':
        return 'audio/mpeg';
      case 'ogg':
        return 'audio/ogg';
      case 'flac':
        return 'audio/flac';
      case 'aac':
        return 'audio/aac';
      default:
        throw StorageException(
            '请选择标准 WAV/M4A/MP3/Ogg/FLAC/AAC；Clip 原始 Opus 需先转换');
    }
  }

  Future<Map<String, dynamic>> upload(File source, Directory cache,
      {void Function(double)? progress, String? filename}) async {
    final uploadName = filename ?? source.uri.pathSegments.last;
    final mime = mimeFor(uploadName);
    final size = await source.length();
    final sha = await hash(source);
    _check();
    await cache.create(recursive: true);
    final key = sha256
        .convert(utf8.encode('${base.origin}|${source.absolute.path}|$sha'))
        .toString();
    final stateFile = File('${cache.path}/upload-$key.json');
    Map<String, dynamic>? info;
    if (await stateFile.exists()) {
      try {
        final saved =
            jsonDecode(await stateFile.readAsString()) as Map<String, dynamic>;
        info = await json('GET', '/api/audio/uploads/${saved['id']}');
      } on StorageException catch (e) {
        if (e.status != 404 && e.status != 410) rethrow;
      }
    }
    info ??= await json('POST', '/api/audio/uploads', body: {
      'filename': uploadName,
      'size': size,
      'sha256': sha,
      'mime': mime,
    });
    final uid = info['id'] as String;
    final tmp = File('${stateFile.path}.tmp');
    await tmp.writeAsString(jsonEncode({'id': uid}), flush: true);
    await tmp.rename(stateFile.path);
    if (info['state'] == 'complete') {
      progress?.call(1);
      return info;
    }
    if (info['state'] != 'finalizing') {
      final chunk = info['chunk_size'] as int;
      if (chunk <= 0 || chunk > 16 * 1024 * 1024) {
        throw StorageException('服务分片大小不受支持');
      }
      final done = {
        for (final p in (info['parts'] as List))
          p['index'] as int: p['sha256'] as String
      };
      final file = await source.open();
      try {
        for (var index = 0, offset = 0;
            offset < size;
            index++, offset += chunk) {
          _check();
          final bytes = await file.read(min(chunk, size - offset));
          if (bytes.length != min(chunk, size - offset)) {
            throw StorageException('源文件长度已改变，请重新上传');
          }
          final partSha = sha256.convert(bytes).toString();
          if (done[index] != partSha) {
            for (var attempt = 0;; attempt++) {
              try {
                final r = await _request(
                    'PUT', '/api/audio/uploads/$uid/parts/$index');
                r.headers.set('X-Content-SHA256', partSha);
                r.contentLength = bytes.length;
                r.add(bytes);
                final response =
                    await r.close().timeout(const Duration(seconds: 120));
                final status = response.statusCode;
                await response
                    .drain<void>()
                    .timeout(const Duration(seconds: 30));
                if (status != 200) {
                  throw StorageException('分片上传失败（HTTP $status）', status);
                }
                break;
              } catch (e) {
                _check();
                if (attempt >= 2 ||
                    (e is StorageException &&
                        e.status != 409 &&
                        (e.status ?? 0) < 500)) {
                  rethrow;
                }
                await Future<void>.delayed(Duration(seconds: 1 << attempt));
              }
            }
          }
          progress?.call(min(0.99, (offset + bytes.length) / size));
        }
      } finally {
        await file.close();
      }
      if (await source.length() != size || await hash(source) != sha) {
        throw StorageException('源文件已改变，请重新上传');
      }
      await json('POST', '/api/audio/uploads/$uid/complete');
    }
    for (var i = 0; i < 600; i++) {
      _check();
      final status = await json('GET', '/api/audio/uploads/$uid');
      if (status['state'] == 'complete') {
        progress?.call(1);
        return status;
      }
      if (status['state'] == 'failed') throw StorageException('服务端整文件校验或落盘失败');
      await Future<void>.delayed(const Duration(seconds: 1));
    }
    throw StorageException('合并仍在进行，请稍后重新查询');
  }

  Future<File> download(Map<String, dynamic> item, Directory directory,
      {void Function(double)? progress}) async {
    final uid = item['id'] as String;
    final expected = item['sha256'] as String;
    final size = item['size'] as int;
    if (!RegExp(r'^[0-9a-f]{32}$').hasMatch(uid) ||
        !RegExp(r'^[0-9a-f]{64}$').hasMatch(expected) ||
        size <= 0) {
      throw StorageException('非法文件元数据');
    }
    final ext = {
      'audio/wav': 'wav',
      'audio/x-wav': 'wav',
      'audio/mp4': 'm4a',
      'audio/mpeg': 'mp3',
      'audio/ogg': 'ogg',
      'audio/flac': 'flac',
      'audio/aac': 'aac'
    }[item['mime']];
    if (ext == null) throw StorageException('不支持的音频格式');
    await directory.create(recursive: true);
    final file = File('${directory.path}/$uid.$ext');
    if (await file.exists() &&
        await file.length() == size &&
        await hash(file) == expected) {
      progress?.call(1);
      return file;
    }
    final partial = File('${file.path}.part');
    var offset = await partial.exists() ? await partial.length() : 0;
    if (offset > size) {
      await partial.delete();
      offset = 0;
    }
    if (offset < size) {
      final r = await _request('GET', '/api/audio/recordings/$uid/content');
      if (offset > 0) {
        r.headers.set('Range', 'bytes=$offset-');
        r.headers.set('If-Range', '"$expected"');
      }
      final response = await r.close().timeout(const Duration(seconds: 30));
      if (response.statusCode != 200 && response.statusCode != 206) {
        await response.drain<void>();
        throw StorageException('下载失败（HTTP ${response.statusCode}）');
      }
      if (response.statusCode == 200) offset = 0;
      if (response.headers.value('etag') != '"$expected"' ||
          (response.statusCode == 206 &&
              response.headers.value('content-range') !=
                  'bytes $offset-${size - 1}/$size')) {
        await response.drain<void>();
        throw StorageException('下载版本或范围不匹配');
      }
      final out = await partial.open(
          mode: offset == 0 ? FileMode.write : FileMode.append);
      try {
        await for (final bytes
            in response.timeout(const Duration(seconds: 30))) {
          _check();
          if (offset + bytes.length > size) throw StorageException('下载长度超过预期');
          await out.writeFrom(bytes);
          offset += bytes.length;
          progress?.call(min(0.99, offset / size));
        }
        await out.flush();
      } finally {
        await out.close();
      }
    }
    if (await partial.length() != size) throw StorageException('下载中断，可重试续传');
    if (await hash(partial) != expected) {
      await partial.delete();
      throw StorageException('下载校验失败，请重新下载');
    }
    await partial.rename(file.path);
    progress?.call(1);
    return file;
  }
}
