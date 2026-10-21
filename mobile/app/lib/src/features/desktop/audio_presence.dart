import 'local_library.dart';

bool matchesAudio(LocalAudio local, Map<String, dynamic> remote) =>
    remote['sha256'] == local.file.uri.pathSegments.last.split('.').first &&
    remote['size'] == local.bytes;

String localPresence(LocalAudio audio, List<Map<String, dynamic>> remote,
        {required bool connected}) =>
    !connected
        ? '本地 · 服务器状态未确认'
        : remote.any((r) => matchesAudio(audio, r))
            ? '本地 + 服务器'
            : '仅本地';

String remotePresence(Map<String, dynamic> audio, List<LocalAudio> local) =>
    local.any((r) => matchesAudio(r, audio)) ? '本地 + 服务器' : '仅服务器';

DateTime remoteTime(Map<String, dynamic> audio) =>
    DateTime.fromMillisecondsSinceEpoch(
        ((audio['created'] as num? ?? 0) * 1000).round(),
        isUtc: true);

String audioTime(DateTime value) {
  if (value.millisecondsSinceEpoch == 0) return '时间未知';
  final d = value.toLocal();
  String two(int v) => v.toString().padLeft(2, '0');
  return '${d.year}-${two(d.month)}-${two(d.day)} ${two(d.hour)}:${two(d.minute)}:${two(d.second)}';
}
