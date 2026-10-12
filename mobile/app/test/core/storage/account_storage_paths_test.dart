import 'package:flutter_test/flutter_test.dart';
import 'package:respeaker_app/src/core/storage/account_storage_paths.dart';

void main() {
  const accountKey = 'user123';
  const currentDocs =
      '/var/mobile/Containers/Data/Application/NEW-UUID/Documents';

  test('remapToCurrentDocumentsRoot rewrites iOS container UUID', () {
    const stored =
        '/var/mobile/Containers/Data/Application/OLD-UUID/Documents/'
        'recordings_user123/device/dev1/sess1.opus';
    expect(
      AccountStoragePaths.remapToCurrentDocumentsRoot(stored, currentDocs),
      '$currentDocs/recordings_user123/device/dev1/sess1.opus',
    );
  });

  test('remapToCurrentDocumentsRoot leaves paths already under current docs', () {
    const stored = '$currentDocs/recordings_user123/a.opus';
    expect(
      AccountStoragePaths.remapToCurrentDocumentsRoot(stored, currentDocs),
      stored,
    );
  });

  test('remapToCurrentDocumentsRoot remaps Android app_flutter prefix', () {
    const stored =
        '/data/user/0/cc.seeed.voice/app_flutter/recordings_user123/a.opus';
    const docs = '/data/user/0/cc.seeed.voice/app_flutter';
    expect(
      AccountStoragePaths.remapToCurrentDocumentsRoot(stored, docs),
      stored,
    );

    const movedDocs = '/data/user/10/cc.seeed.voice/app_flutter';
    expect(
      AccountStoragePaths.remapToCurrentDocumentsRoot(stored, movedDocs),
      '$movedDocs/recordings_user123/a.opus',
    );
  });

  test('localPathRewriteCandidates tries original then legacy then remap', () {
    const stored =
        '/var/mobile/Containers/Data/Application/OLD-UUID/Documents/'
        'recordings/device/dev1/sess1.opus';
    final candidates = AccountStoragePaths.localPathRewriteCandidates(
      storedPath: stored,
      accountKey: accountKey,
      documentsRoot: currentDocs,
    );

    expect(candidates.first, stored);
    expect(
      candidates,
      contains(
        '/var/mobile/Containers/Data/Application/OLD-UUID/Documents/'
        'recordings_user123/device/dev1/sess1.opus',
      ),
    );
    expect(
      candidates,
      contains('$currentDocs/recordings/device/dev1/sess1.opus'),
    );
    expect(
      candidates.last,
      '$currentDocs/recordings_user123/device/dev1/sess1.opus',
    );
  });
}
