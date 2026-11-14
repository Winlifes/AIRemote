#!/usr/bin/env python3
"""Register AIRemote's local companion in the installed DeepSeek profile."""
from datetime import datetime
import json
from pathlib import Path
import plistlib
import shutil


def install(project):
    project = Path(project).resolve(strict=True)
    app = Path('/Applications/DeepSeek Harness.app')
    info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
    if info.get('CFBundleShortVersionString') != '0.2.2-rc.1':
        raise RuntimeError('DeepSeek installed version requires adapter validation')
    profile = Path.home() / '.dsh/profiles/desktop/cordis.patch.yml'
    original = profile.read_text()
    marker = '# AIRemote same-Host connection'
    if marker in original:
        return {'installed': True, 'reused': True}
    private = project / '.local'; private.mkdir(mode=0o700, exist_ok=True)
    backup = private / ('deepseek-profile-before-airemote-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.yml')
    shutil.copyfile(profile, backup); backup.chmod(0o600)
    entry = '\n' + marker + '\n- insert:\n    - id: airemote-local-connection\n      name: ' + json.dumps(str(project / 'deploy/deepseek-link.mjs')) + '\n      config:\n        connectionFile: ' + json.dumps(str(private / 'deepseek-connection.json')) + '\n'
    profile.write_text(original + entry)
    return {'installed': True, 'reused': False, 'backup': str(backup)}


if __name__ == '__main__':
    print(json.dumps(install(Path(__file__).resolve().parents[1])))
