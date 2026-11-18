#!/usr/bin/env python3
"""Build a portable, ad-hoc signed Mac app with a private-data-free runtime."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess
import zipfile

PROJECT = Path(__file__).resolve().parents[1]


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode: raise RuntimeError(result.stderr or result.stdout)


def bundle_runtime(destination):
    files = {
        'poc': ['app_server_rpc.py', 'claude_adapter.py', 'codex_probe.py', 'control_server.py', 'deepseek_adapter.py', 'desktop_ipc.py', 'project_catalog.py', 'goal_transport.py'],
        'relay': ['agent.py', 'client.py', 'protocol.py'],
        'deploy': ['start_mac.py', 'configure_mac.py', 'install_deepseek.py', 'deepseek-link.mjs', 'update_mac.py', 'update_core.py', 'setup_goal_channel.py'],
    }
    for directory, names in files.items():
        target = destination / directory; target.mkdir(parents=True, exist_ok=True)
        for name in names: shutil.copy2(PROJECT / directory / name, target / name)
    for name in ['VERSION', 'LICENSE', 'THIRD_PARTY_NOTICES.md']:
        shutil.copy2(PROJECT / name, destination / name)


def archive_app(app, archive):
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(app.rglob('*')):
            if path.is_symlink(): raise ValueError('Unexpected symlink in app bundle')
            if path.is_file(): output.write(path, path.relative_to(app.parent))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PROJECT / 'dist/AIRemote.app')
    parser.add_argument('--zip', type=Path)
    args = parser.parse_args()
    target = args.output.expanduser().resolve()
    if target.name != 'AIRemote.app': raise ValueError('Output must be AIRemote.app')
    if target.exists():
        info = plistlib.loads((target / 'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') != 'fun.winlife.airemote': raise ValueError('Refusing to replace another application')
    staging = PROJECT / 'build/mac/AIRemote.app'
    if staging.exists(): shutil.rmtree(staging)
    macos = staging / 'Contents/MacOS'; resources = staging / 'Contents/Resources'
    macos.mkdir(parents=True); resources.mkdir(parents=True)
    binaries = []
    for arch in ['arm64', 'x86_64']:
        binary = staging.parent / ('AIRemote-' + arch)
        run('xcrun', 'swiftc', '-swift-version', '5', '-parse-as-library', '-O', '-g', '-debug-prefix-map', str(PROJECT) + '=/AIRemote',
            '-target', arch + '-apple-macosx13.0', str(PROJECT / 'macos/AIRemoteApp.swift'), str(PROJECT / 'macos/UpdaterProcess.swift'), '-o', str(binary))
        binaries.append(str(binary))
    run('xcrun', 'lipo', '-create', *binaries, '-output', str(macos / 'AIRemote'))
    run('xcrun', 'strip', '-S', str(macos / 'AIRemote'))
    png = resources / 'AIRemote.png'
    run('/usr/bin/sips', '-s', 'format', 'png', '-z', '1024', '1024', str(PROJECT / 'web/public/icon.svg'), '--out', str(png))
    iconset = staging.parent / 'AIRemote.iconset'; iconset.mkdir(exist_ok=True)
    for size in [16, 32, 128, 256, 512]:
        for density in [1, 2]:
            name = f'icon_{size}x{size}' + ('@2x' if density == 2 else '') + '.png'
            run('/usr/bin/sips', '-z', str(size*density), str(size*density), str(png), '--out', str(iconset / name))
    run('/usr/bin/iconutil', '-c', 'icns', str(iconset), '-o', str(resources / 'AIRemote.icns'))
    for provider in ['codex', 'deepseek']:
        shutil.copy2(PROJECT / f'web/public/providers/{provider}.png', resources / f'{provider}.png')
    bundle_runtime(resources / 'runtime')
    version = (PROJECT / 'VERSION').read_text().strip()
    info = {'CFBundleIdentifier': 'fun.winlife.airemote', 'CFBundleName': 'AIRemote', 'CFBundleDisplayName': 'AIRemote',
        'CFBundleExecutable': 'AIRemote', 'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': version,
        'CFBundleVersion': version, 'CFBundleIconFile': 'AIRemote.icns', 'LSMinimumSystemVersion': '13.0',
        'NSHighResolutionCapable': True, 'NSDocumentsFolderUsageDescription': 'AIRemote 需要访问已授权的 AI 项目、配置和产物。'}
    (staging / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    run('/usr/bin/codesign', '--force', '--sign', '-', str(staging))
    run('/usr/bin/codesign', '--verify', '--deep', '--strict', str(staging))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists(): shutil.rmtree(target)
    shutil.copytree(staging, target)
    if args.zip:
        args.zip.parent.mkdir(parents=True, exist_ok=True); archive_app(target, args.zip)
    print(str(target))


if __name__ == '__main__': main()
