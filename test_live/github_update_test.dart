// Optional check of the currently published GitHub update feed.
// ignore_for_file: avoid_print
import 'dart:io';
import 'package:flutter_test/flutter_test.dart';
import 'package:airemote_mobile/core/updates.dart';

void main() {
  test(
    'actual stable GitHub APK downloads with progress and passes SHA-256',
    () async {
      final source = GitHubReleases();
      final directory = await Directory.systemTemp.createTemp('airemote-feed-');
      try {
        final release = await source.latest();
        final events = <(int, int?)>[];
        final file = await source.download(
          release,
          directory,
          (bytes, total, _) => events.add((bytes, total)),
        );
        expect(events.first.$1, 0);
        expect(events.last.$1, await file.length());
        expect(events.last.$2, release.size);
        expect(events.length, greaterThanOrEqualTo(2));
        // download() only renames the partial file after verifying the release hash.
        expect(file.path.endsWith('.apk'), true);
        print(
          'Verified ${release.version}: ${await file.length()} bytes, ${events.length} progress events',
        );
      } finally {
        source.close();
        await directory.delete(recursive: true);
      }
    },
    timeout: const Timeout(Duration(minutes: 4)),
  );
}
