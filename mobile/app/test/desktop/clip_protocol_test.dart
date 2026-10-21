import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/desktop/clip_protocol.dart';

void main() {
  test('only acknowledged commands succeed', () {
    expect(() => checkedClipReply({'ok': false, 'error': 'unsupported'}),
        throwsStateError);
    expect(() => checkedClipReply({'state': 'recording'}), throwsStateError);
    expect(checkedClipReply({'ok': true, 'state': 'idle'})['state'], 'idle');
  });
  test('desktop uses only supported recording commands', () {
    expect(clipRecordingCommands.values, contains('AT+MARK'));
    expect(
        clipRecordingCommands.values
            .any((s) => s.contains('PURGE') || s.contains('MARK=')),
        false);
  });
}
