#!/usr/bin/env python3
"""Build the local native Mac launcher; private runtime config stays outside it."""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess


def run(*args):
    subprocess.run(args, check=True, capture_output=True, text=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',default='/Applications/AIRemote.app')
    args=parser.parse_args()
    project=Path(__file__).resolve().parents[1]
    target=Path(args.output).expanduser().resolve()
    if target.suffix!='.app' or target.name!='AIRemote.app':raise ValueError('Output must be AIRemote.app')
    if target.exists():
        info=plistlib.loads((target/'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier')!='fun.winlife.airemote':raise ValueError('Refusing to replace another application')
    staging=project/'.local/mac-app-build/AIRemote.app'
    if staging.exists():shutil.rmtree(staging)
    macos=staging/'Contents/MacOS';resources=staging/'Contents/Resources'
    macos.mkdir(parents=True);resources.mkdir(parents=True)
    run('xcrun','swiftc','-swift-version','5','-parse-as-library','-O','-target','arm64-apple-macosx13.0',str(project/'macos/AIRemoteApp.swift'),'-o',str(macos/'AIRemote'))
    png=resources/'AIRemote.png'
    run('/usr/bin/sips','-s','format','png','-z','1024','1024',str(project/'web/public/icon.svg'),'--out',str(png))
    iconset=project/'.local/mac-app-build/AIRemote.iconset';iconset.mkdir(exist_ok=True)
    for size in [16,32,128,256,512]:
        for density in [1,2]:
            name=f'icon_{size}x{size}'+('@2x' if density==2 else '')+'.png'
            run('/usr/bin/sips','-z',str(size*density),str(size*density),str(png),'--out',str(iconset/name))
    run('/usr/bin/iconutil','-c','icns',str(iconset),'-o',str(resources/'AIRemote.icns'))
    for provider in ['codex','deepseek']:shutil.copyfile(project/f'web/public/providers/{provider}.png',resources/f'{provider}.png')
    info={'CFBundleIdentifier':'fun.winlife.airemote','CFBundleName':'AIRemote','CFBundleDisplayName':'AIRemote','CFBundleExecutable':'AIRemote','CFBundlePackageType':'APPL','CFBundleShortVersionString':'0.5.1','CFBundleVersion':'1','CFBundleIconFile':'AIRemote.icns','LSMinimumSystemVersion':'13.0','NSHighResolutionCapable':True,'NSDocumentsFolderUsageDescription':'AIRemote 需要访问项目目录中的启动配置和已授权的 AI 产物。','AIRemoteProjectDirectory':str(project)}
    (staging/'Contents/Info.plist').write_bytes(plistlib.dumps(info))
    run('/usr/bin/codesign','--force','--sign','-',str(staging))
    target.parent.mkdir(parents=True,exist_ok=True)
    if target.exists():shutil.rmtree(target)
    shutil.copytree(staging,target)
    print(str(target))


if __name__=='__main__':main()
