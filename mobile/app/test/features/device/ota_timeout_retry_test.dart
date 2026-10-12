import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/features/device/presentation/firmware_update_page.dart';

void main() {
  test('retries Nordic iOS encoder-IO timeout', () {
    expect(
      isRetryableOtaTimeout(
        Exception('The encoder IO timed out without receiving a response'),
      ),
      isTrue,
    );
  });

  test('retries Android SMP transaction timeout', () {
    expect(
      isRetryableOtaTimeout(
        Exception('Transaction 54 timed out without receiving a response'),
      ),
      isTrue,
    );
  });

  test('does not retry parse or user-cancel errors', () {
    expect(isRetryableOtaTimeout(Exception('Firmware parsing failed')), isFalse);
    expect(isRetryableOtaTimeout(Exception('Cancelled')), isFalse);
  });
}
