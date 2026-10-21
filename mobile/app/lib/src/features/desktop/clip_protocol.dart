const clipRecordingCommands = {
  '开始录音': 'AT+START',
  '暂停': 'AT+PAUSE',
  '继续': 'AT+RESUME',
  '停止录音': 'AT+STOP',
  '添加标记': 'AT+MARK',
};
Map<String, dynamic> checkedClipReply(Map<String, dynamic> reply) {
  if (reply['ok'] != true) {
    throw StateError(
        '设备未确认成功：${reply['error'] ?? reply['message'] ?? '请刷新状态后重试'}');
  }
  return reply;
}
