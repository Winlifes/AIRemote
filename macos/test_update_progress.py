"""Compile and exercise the same streaming reader used by the Mac app."""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
HARNESS = r'''
import Foundation
@main struct Check {
    @MainActor static func main() async {
        let script = CommandLine.arguments[1]
        var received = [UpdateProgress](), elapsed = [Double]()
        let start = Date()
        let result = await UpdaterProcess.run([script], timeout: 5) { value in
            received.append(value); elapsed.append(Date().timeIntervalSince(start))
        }
        precondition(result.0 == 0, result.1)
        precondition(result.1 == "{\"available\":false,\"version\":\"0.8.0\"}")
        precondition(received.count == 4)
        precondition(received[0].fraction == nil)
        precondition(received[1].fraction == 0.5)
        precondition(received[1].detail!.contains("1.0 KB / 2.0 KB"))
        precondition(received[2].fraction == nil && received[2].title == "正在下载更新…")
        precondition(received[3].stage == "verifying")
        // First delivery must occur while the child is still downloading.
        precondition(elapsed[1] < elapsed.last! - 0.15)
        let failure = await UpdaterProcess.run(["-c", "print('{\\\"error\\\":\\\"checksum mismatch\\\"}');raise SystemExit(2)"])
        precondition(failure.0 == 2 && failure.1.contains("checksum mismatch"))
        let timeout = await UpdaterProcess.run(["-c", "import time;time.sleep(5)"], timeout: 0.1)
        precondition(timeout.0 != 0 && timeout.1.contains("超时"))
        print("Mac live progress, indeterminate transfer, failure and timeout passed")
    }
}
'''
CHILD = '''import json,sys,time
def emit(stage, **values):
    print(json.dumps(dict(event='update-progress', stage=stage, **values)), flush=True)
emit('checking')
emit('downloading',downloadedBytes=1024,totalBytes=2048,bytesPerSecond=512)
time.sleep(.35)
emit('downloading',downloadedBytes=4096,totalBytes=None,bytesPerSecond=1024)
emit('verifying')
print('{"available":false,"version":"0.8.0"}',flush=True)
'''

if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='airemote-progress-') as directory:
        directory = Path(directory)
        (directory / 'Check.swift').write_text(HARNESS)
        (directory / 'child.py').write_text(CHILD)
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', str(ROOT / 'UpdaterProcess.swift'), str(directory / 'Check.swift'), '-o', str(directory / 'check')], check=True)
        subprocess.run([str(directory / 'check'), str(directory / 'child.py')], check=True, timeout=20)
