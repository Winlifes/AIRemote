"""GitHub release metadata, integrity checks and bounded archive extraction."""
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import time
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
import zipfile

DEFAULT_REPO = 'Winlifes/AIRemote'
MAX_DOWNLOAD = 512 * 1024 * 1024


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'v?[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}', value):
        raise ValueError('Not a stable semantic version')
    return tuple(int(part) for part in value.lstrip('v').split('.'))


def github_url(value):
    url = urlsplit(value)
    host = (url.hostname or '').lower()
    if url.scheme != 'https' or url.username or url.password or url.port not in {None, 443} or not (
            host in {'api.github.com', 'github.com'} or host.endswith('.githubusercontent.com')):
        raise ValueError('Update URLs must use trusted GitHub HTTPS hosts')
    return value


class GitHubRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        github_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def progress_event(callback, stage, **values):
    if callback:
        callback({'event': 'update-progress', 'stage': stage, **values})


def download(url, target=None, limit=MAX_DOWNLOAD, progress=None, expected_size=None):
    # Transient CDN failures must never switch the installed release. Retry a
    # fresh bounded transfer; integrity checks still happen after completion.
    for attempt in range(3):
        try: return _download_once(url, target, limit, progress, expected_size)
        except (URLError, TimeoutError, ConnectionError, IncompleteRead) as error:
            if isinstance(error, HTTPError) and error.code not in {429, 500, 502, 503, 504}: raise
            if attempt == 2: raise
            progress_event(progress, 'retrying', attempt=attempt + 2)
            time.sleep(.5 * (2 ** attempt))


def _download_once(url, target=None, limit=MAX_DOWNLOAD, progress=None, expected_size=None):
    github_url(url)
    request = Request(url, headers={'User-Agent': 'AIRemote-Updater', 'Accept': 'application/vnd.github+json'})
    opener = build_opener(GitHubRedirects())
    digest = hashlib.sha256(); total = 0; chunks = []
    output = open(target, 'wb') if target else None
    try:
        progress_event(progress, 'connecting', downloadedBytes=0)
        with opener.open(request, timeout=45) as response:
            try: length = int(response.headers.get('Content-Length', '0'))
            except (TypeError, ValueError): length = 0
            length = length if length > 0 else expected_size
            if length is not None and length > limit:
                raise ValueError('Release download exceeds size limit')
            started = last_event = time.monotonic()
            progress_event(progress, 'downloading', downloadedBytes=0, totalBytes=length, bytesPerSecond=0)
            while True:
                block = response.read(65536)
                if not block: break
                total += len(block)
                if total > limit: raise ValueError('Release download exceeds size limit')
                digest.update(block)
                if output: output.write(block)
                else: chunks.append(block)
                current = time.monotonic()
                if current - last_event >= .1:
                    progress_event(progress, 'downloading', downloadedBytes=total, totalBytes=length,
                                   bytesPerSecond=total / max(current - started, .001))
                    last_event = current
            progress_event(progress, 'downloading', downloadedBytes=total, totalBytes=length,
                           bytesPerSecond=total / max(time.monotonic() - started, .001))
    except BaseException:
        if output: output.close(); Path(target).unlink(missing_ok=True)
        raise
    finally:
        if output and not output.closed: output.close()
    return digest.hexdigest() if target else b''.join(chunks)


def latest_release(repo=DEFAULT_REPO, progress=None):
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo): raise ValueError('Invalid GitHub repository')
    progress_event(progress, 'checking')
    release = json.loads(download('https://api.github.com/repos/' + repo + '/releases/latest', limit=2 * 1024 * 1024))
    version_tuple(release['tag_name'])
    if release.get('draft') or release.get('prerelease'): raise ValueError('Stable updates cannot install a pre-release')
    return release


def release_asset(release, name, repo=DEFAULT_REPO):
    assets = {asset['name']: asset for asset in release.get('assets', [])}
    if name not in assets: raise ValueError('Release is missing ' + name)
    asset = assets[name]
    expected_url = 'https://github.com/' + repo + '/releases/download/' + release['tag_name'] + '/' + name
    if asset.get('browser_download_url') != expected_url: raise ValueError('Unexpected release asset URL')
    digest = asset.get('digest') or ''
    if re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest): digest = digest[7:]
    else:
        manifest = assets.get('SHA256SUMS')
        if not manifest: raise ValueError('Release has no integrity manifest')
        expected_manifest = expected_url.rsplit('/', 1)[0] + '/SHA256SUMS'
        if manifest['browser_download_url'] != expected_manifest: raise ValueError('Unexpected checksum URL')
        digest = None
        for line in download(expected_manifest, limit=65536).decode().splitlines():
            fields = line.split(maxsplit=1)
            if len(fields) == 2 and fields[1].lstrip('*') == name: digest = fields[0]
    if not digest or not re.fullmatch(r'[0-9a-fA-F]{64}', digest): raise ValueError('Asset has no valid SHA-256 digest')
    size = asset.get('size')
    return {'name': name, 'url': expected_url, 'sha256': digest.lower(),
            'size': size if type(size) is int and size > 0 else None}


def fetch_asset(asset, directory, progress=None):
    directory = Path(directory); directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / asset['name']; partial = target.with_suffix(target.suffix + '.partial')
    actual = download(asset['url'], partial, progress=progress, expected_size=asset.get('size')) if progress else download(asset['url'], partial)
    progress_event(progress, 'verifying')
    if actual != asset['sha256']:
        partial.unlink(missing_ok=True); raise ValueError('Release checksum mismatch; installation stopped')
    partial.replace(target)
    return target


def archive_path(name, root):
    path = PurePosixPath(name)
    if not name or '\\' in name or path.is_absolute() or '..' in path.parts:
        raise ValueError('Unsafe archive path')
    target = root.joinpath(*path.parts).resolve()
    if not target.is_relative_to(root): raise ValueError('Archive escapes staging directory')
    return target


def extract_zip(archive, directory, progress=None):
    progress_event(progress, 'extracting')
    root = Path(directory).resolve(); root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        entries = source.infolist()
        if len(entries) > 10000 or sum(item.file_size for item in entries) > MAX_DOWNLOAD:
            raise ValueError('Expanded update exceeds limits')
        # Validate the whole archive before writing anything.
        for item in entries:
            archive_path(item.filename, root)
            if stat.S_ISLNK(item.external_attr >> 16): raise ValueError('Release symlinks are not supported')
        for item in entries:
            target = archive_path(item.filename, root)
            if item.is_dir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(item) as content, target.open('wb') as output:
                import shutil
                shutil.copyfileobj(content, output)
            target.chmod(((item.external_attr >> 16) & 0o755) or 0o644)


def extract_tar(archive, directory):
    root = Path(directory).resolve(); root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, 'r:gz') as source:
        entries = source.getmembers()
        if len(entries) > 10000 or sum(item.size for item in entries) > MAX_DOWNLOAD:
            raise ValueError('Expanded update exceeds limits')
        for item in entries:
            archive_path(item.name, root)
            if not (item.isfile() or item.isdir()): raise ValueError('Unsupported release archive entry')
        for item in entries:
            target = archive_path(item.name, root)
            if item.isdir(): target.mkdir(parents=True, exist_ok=True); continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(item) as content, target.open('wb') as output:
                import shutil
                shutil.copyfileobj(content, output)
            target.chmod(item.mode & 0o755 or 0o644)
