"""Assemble a game's setup for players: a folder (and zip) that turns the player's own dump into a
playable port without Git, Visual Studio or a Python install (docs D9 of the game projects). It holds:

  setup.bat               runs python\\python.exe -I -u kit\\tools\\setup.py
  README.txt              what the setup does, and where the game ends up
  python\\                 Python's embeddable distribution with Tk (python.org's tcltk.msi, for the
                          setup's window), capstone and PyYAML (versions pinned: the recompiler's
                          output depends on capstone's)
  kit\\recomp, kit\\tools   this kit's recompiler and tools
  kit\\game\\              the game's front end (a VITA3K_GAMES_ONLY build), Vita3K's data, game.json
                          and the README of the playable folder: the template the setup installs
                          from, kept out of sight so no one starts its exe

Nothing derived from a game or the firmware goes in: release checks refuse such files.

Linux (a setup-linux.json lock, run on Linux): setup.sh, python/ from python-build-standalone (Tk
included) with the manylinux wheels, kit/ as above with <id>.x86_64; --zip makes a .tar.gz.

macOS (a setup-macos.json lock, run on an Apple Silicon Mac): setup.command, python/ from
python-build-standalone (Tk included) with the macOS arm64 wheels, kit/ as above with <id>.arm64 and
libMoltenVK.dylib; --zip makes a .tar.gz. Nothing is signed with a developer ID: setup.command, the
one file the player opens through Gatekeeper ("Open Anyway"), clears the download's quarantine from
the rest of its folder.

usage:
  python -I make-setup.py resolve <lock.json>           pin the embeddable Python and the wheels
  python -I make-setup.py resolve-linux <lock.json> <tag>   pin python-build-standalone release <tag>
  python -I make-setup.py resolve-macos <lock.json> <tag>   the same for Apple Silicon macOS
  python -I make-setup.py build <lock.json> --project DIR --bin DIR --out DIR [--zip]
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile
import sys
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from gameproject import Project  # noqa: E402

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = '3.12.10'
WHEELS = {'capstone': '5.0.7', 'PyYAML': '6.0.3'}
# what the front end's folder may hold; anything else (logs, dumps, saves, recompiled modules) stays out
BIN_FILES = re.compile(r'^(\w[\w-]*\.exe|discord_game_sdk\.dll|lib(ssl|crypto)-\d+-x64\.dll|COPYING\.txt)$')
BIN_DIRS = ('data', 'shaders-builtin', 'icons')
# files a setup must never contain
FORBIDDEN = re.compile(r'(?i)(\.(elf|self|suprx|pkg|pup|rif|vpk|bin|psarc|log)$|work\.bin|ssd_aot_|elfdumps|savedata)')


# DLLs a game's folder need not carry: the VC++ runtime (the setup copies it from Microsoft's package)
VC_RUNTIME = re.compile(r'(?i)^(msvcp140.*|vcruntime140.*|concrt140)\.dll$')

# Linux: Python from python-build-standalone (with Tk), the same wheels as manylinux builds
PBS_REPO = 'astral-sh/python-build-standalone'
LINUX_BIN_FILES = re.compile(r'^(\w[\w-]*\.x86_64|\w[\w-]*\.ico|COPYING\.txt)$')
MACOS_BIN_FILES = re.compile(r'^(\w[\w-]*\.arm64|\w[\w-]*\.ico|libMoltenVK\.dylib|COPYING\.txt)$')
LINUX_SYSTEM_LIBS = {'libc.so.6', 'libm.so.6', 'libdl.so.2', 'libpthread.so.0', 'librt.so.1',
                     'ld-linux-x86-64.so.2', 'libz.so.1'}


def pe_imports(path):
    """The DLL names a PE32+ file imports, normal and delay-loaded."""
    with open(path, 'rb') as f:
        d = f.read()
    pe = struct.unpack_from('<I', d, 0x3c)[0]
    nsec, optsz = struct.unpack_from('<H', d, pe + 6)[0], struct.unpack_from('<H', d, pe + 20)[0]
    opt = pe + 24
    secs = [struct.unpack_from('<8sIIII', d, opt + optsz + 40 * i) for i in range(nsec)]

    def off(rva):
        for _, vsz, va, rsz, ptr in secs:
            if va <= rva < va + max(vsz, rsz):
                return rva - va + ptr
        raise ValueError(hex(rva))

    names = []
    for idx, size, name_at in ((1, 20, 12), (13, 32, 4)):
        rva = struct.unpack_from('<I', d, opt + 112 + 8 * idx)[0]
        o = off(rva) if rva else None
        while o is not None:
            name = struct.unpack_from('<I', d, o + name_at)[0]
            if not name:
                break
            n = off(name)
            names.append(d[n:d.index(b'\0', n)].decode())
            o += size
    return names


def missing_dlls(exe):
    """Imported DLLs that are neither beside the exe, part of Windows, nor the VC++ runtime."""
    system = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
    here = {f.lower() for f in os.listdir(os.path.dirname(exe))}
    return sorted(n for n in pe_imports(exe)
                  if n.lower() not in here and not n.lower().startswith(('api-ms-', 'ext-ms-'))
                  and not VC_RUNTIME.match(n) and not os.path.exists(os.path.join(system, n)))


def elf_needed(path):
    """The DT_NEEDED libraries of an x86-64 ELF file."""
    with open(path, 'rb') as f:
        d = f.read()
    if d[:4] != b'\x7fELF' or d[4] != 2:
        raise ValueError(f'{path}: not a 64-bit ELF file')
    phoff, = struct.unpack_from('<Q', d, 0x20)
    phentsize, phnum = struct.unpack_from('<HH', d, 0x36)
    loads, dynamic = [], None
    for i in range(phnum):
        p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from('<IIQQQQQQ', d, phoff + i * phentsize)
        if p_type == 1:  # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:  # PT_DYNAMIC
            dynamic = (p_offset, p_filesz)
    if not dynamic:
        return []

    def off(vaddr):
        return next(o + vaddr - v for v, o, sz in loads if v <= vaddr < v + sz)

    needed, strtab = [], None
    for o in range(dynamic[0], dynamic[0] + dynamic[1], 16):
        tag, val = struct.unpack_from('<qQ', d, o)
        if tag == 0:
            break
        if tag == 1:  # DT_NEEDED
            needed.append(val)
        elif tag == 5:  # DT_STRTAB
            strtab = off(val)
    return [d[strtab + n:d.index(b'\0', strtab + n)].decode() for n in needed]


def missing_libs(exe):
    """Linux: libraries the executable needs beyond glibc and zlib, which every distribution has."""
    return sorted(n for n in elf_needed(exe) if n not in LINUX_SYSTEM_LIBS)


def get(url, path=None):
    req = urllib.request.Request(url, headers={'User-Agent': 'vita-recomp-kit make-setup.py'})
    with urllib.request.urlopen(req, timeout=120) as r:
        if path is None:
            return r.read()
        with open(path, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def resolve(lock_path, cache):
    os.makedirs(cache, exist_ok=True)
    files = []
    url = f'https://www.python.org/ftp/python/{PYTHON}/python-{PYTHON}-embed-amd64.zip'
    local = os.path.join(cache, os.path.basename(url))
    get(url, local)
    files.append({'kind': 'python', 'url': url, 'sha256': sha256(local), 'size': os.path.getsize(local)})
    # Tk, which the embeddable distribution leaves out: the Windows installer's own component
    url = f'https://www.python.org/ftp/python/{PYTHON}/amd64/tcltk.msi'
    local = os.path.join(cache, os.path.basename(url))
    get(url, local)
    files.append({'kind': 'tcltk', 'url': url, 'sha256': sha256(local), 'size': os.path.getsize(local)})
    tag = 'cp' + ''.join(PYTHON.split('.')[:2])
    for name, version in WHEELS.items():
        release = json.loads(get(f'https://pypi.org/pypi/{name}/{version}/json'))
        wheels = [u for u in release['urls'] if u['filename'].endswith('.whl') and 'win_amd64' in u['filename']
                  and (f'-{tag}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not wheels:
            sys.exit(f'{name} {version}: no Windows x64 wheel for {tag}')
        w = wheels[0]
        files.append({'kind': 'wheel', 'url': w['url'], 'sha256': w['digests']['sha256'], 'size': w['size']})
    lock = {'schema': 'vita-recomp-kit.setup.v1', 'python': PYTHON, 'wheels': WHEELS, 'files': files}
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: Python {PYTHON}, ' + ', '.join(f'{k} {v}' for k, v in WHEELS.items()))


def resolve_linux(lock_path, cache, tag, platform='linux'):
    """Pin python-build-standalone's CPython 3.12 of release <tag> (Tk included) and the wheels, for
    x86-64 Linux or (platform 'macos') Apple Silicon macOS."""
    os.makedirs(cache, exist_ok=True)
    release = json.loads(get(f'https://api.github.com/repos/{PBS_REPO}/releases/tags/{tag}'))
    triple = 'aarch64-apple-darwin' if platform == 'macos' else 'x86_64-unknown-linux-gnu'
    pattern = re.compile(r'^cpython-(3\.12\.\d+)\+' + re.escape(tag) + '-' + triple + r'-install_only\.tar\.gz$')
    asset = next((x for x in release['assets'] if pattern.match(x['name'])), None)
    if not asset:
        sys.exit(f'{PBS_REPO} {tag}: no CPython 3.12 install_only build for {triple}')
    version = pattern.match(asset['name']).group(1)
    sums = get(f'https://github.com/{PBS_REPO}/releases/download/{tag}/SHA256SUMS').decode()
    digest = next(line.split()[0] for line in sums.splitlines() if line.endswith(' ' + asset['name']))
    files = [{'kind': 'python', 'url': asset['browser_download_url'], 'sha256': digest, 'size': asset['size']}]
    tag_py = 'cp' + ''.join(version.split('.')[:2])

    def fits(fn):
        if platform == 'macos':  # arm64 or universal2 (both architectures) macOS wheels
            return 'macosx' in fn and ('arm64' in fn or 'universal2' in fn)
        return 'manylinux' in fn and 'x86_64' in fn
    for name, wversion in WHEELS.items():
        info = json.loads(get(f'https://pypi.org/pypi/{name}/{wversion}/json'))
        wheels = [u for u in info['urls'] if u['filename'].endswith('.whl') and fits(u['filename'])
                  and (f'-{tag_py}-' in u['filename'] or '-py3-none-' in u['filename'])]
        if not wheels:
            sys.exit(f'{name} {wversion}: no {triple} wheel for {tag_py}')
        w = wheels[0]
        files.append({'kind': 'wheel', 'url': w['url'], 'sha256': w['digests']['sha256'], 'size': w['size']})
    lock = {'schema': 'vita-recomp-kit.setup.v1', 'platform': platform, 'python': version,
            'python_build_standalone': tag, 'wheels': WHEELS, 'files': files}
    with open(lock_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(lock, f, indent=1)
    print(f'{lock_path}: Python {version} ({PBS_REPO} {tag}), ' + ', '.join(f'{k} {v}' for k, v in WHEELS.items()))


def fetch(lock, cache):
    os.makedirs(cache, exist_ok=True)
    paths = {}
    for f in lock['files']:
        local = os.path.join(cache, os.path.basename(f['url']))
        if not (os.path.exists(local) and sha256(local) == f['sha256']):
            get(f['url'], local)
            if sha256(local) != f['sha256']:
                sys.exit(f'{local}: wrong SHA-256')
        paths.setdefault(f['kind'], []).append(local)
    return paths


def copy_tree(src, dst, skip=('__pycache__', 'tests')):
    for d, dirs, files in os.walk(src):
        dirs[:] = [x for x in dirs if x not in skip and not x.startswith('.')]
        rel = os.path.relpath(d, src)
        for f in files:
            if f.endswith(('.pyc', '.ps1')):
                continue
            os.makedirs(os.path.join(dst, rel), exist_ok=True)
            shutil.copy2(os.path.join(d, f), os.path.join(dst, rel, f))


def release_check(out):
    """Refuse a setup holding anything that looks like game or firmware data."""
    bad = [os.path.relpath(os.path.join(d, f), out) for d, _, fs in os.walk(out) for f in fs
           if FORBIDDEN.search(os.path.relpath(os.path.join(d, f), out).replace('\\', '/'))
           and not os.path.relpath(d, out).startswith('python')]
    if bad:
        shutil.rmtree(out)
        sys.exit('refused, files that look like game data: ' + ', '.join(bad[:10]))


def copy_kit(out):
    copy_tree(os.path.join(KIT, 'recomp'), os.path.join(out, 'kit', 'recomp'))
    os.makedirs(os.path.join(out, 'kit', 'tools'))
    for f in glob.glob(os.path.join(KIT, 'tools', '*.py')) + glob.glob(os.path.join(KIT, 'tools', '*.json')):
        shutil.copy2(f, os.path.join(out, 'kit', 'tools'))
    shutil.copy2(os.path.join(KIT, 'LICENSE'), os.path.join(out, 'kit', 'LICENSE'))


def python_standalone(lock, out):
    """python/ from python-build-standalone (Linux, macOS), with the wheels in its site-packages."""
    paths = fetch(lock, os.path.join(os.path.dirname(out), 'setup-downloads'))
    py = os.path.join(out, 'python')
    with tarfile.open(paths['python'][0]) as t:
        members = []
        for m in t.getmembers():
            parts = m.name.split('/', 1)
            if parts[0] == 'python' and len(parts) == 2 and parts[1]:
                m.name = parts[1]
                members.append(m)
        t.extractall(py, members, filter='tar')
    site = glob.glob(os.path.join(py, 'lib', 'python3.*', 'site-packages'))[0]
    for w in paths['wheel']:
        with zipfile.ZipFile(w) as z:
            z.extractall(site)
            for info in z.infolist():  # zip keeps no modes: capstone's library must stay loadable
                if info.filename.endswith(('.so', '.dylib')) or '.so.' in info.filename:
                    os.chmod(os.path.join(site, info.filename), 0o755)


def macho_dylibs(path):
    """The libraries an arm64 Mach-O file loads (normal, weak, re-exported and upward), from its load
    commands; a universal file's arm64 slice."""
    with open(path, 'rb') as f:
        d = f.read()
    base = 0
    if struct.unpack_from('>I', d, 0)[0] == 0xcafebabe:  # universal: find the arm64 slice
        for i in range(struct.unpack_from('>I', d, 4)[0]):
            cputype, _, offset, _, _ = struct.unpack_from('>iiIII', d, 8 + 20 * i)
            if cputype == 0x0100000c:
                base = offset
    if struct.unpack_from('<I', d, base)[0] != 0xfeedfacf:
        raise ValueError(f'{path}: not a 64-bit Mach-O file')
    ncmds = struct.unpack_from('<I', d, base + 16)[0]
    names, o = [], base + 32
    for _ in range(ncmds):
        cmd, size = struct.unpack_from('<II', d, o)
        if cmd in (0xc, 0x80000018, 0x8000001f, 0x80000023):  # LC_LOAD_(WEAK_,UPWARD_)DYLIB, LC_REEXPORT_DYLIB
            n = o + struct.unpack_from('<I', d, o + 8)[0]
            names.append(d[n:d.index(b'\0', n)].decode())
        o += size
    return names


def missing_macos_libs(path):
    """macOS: libraries a Mach-O file loads that are neither part of macOS nor beside it."""
    here = os.path.dirname(path)
    return sorted(n for n in macho_dylibs(path)
                  if not n.startswith(('/usr/lib/', '/System/Library/'))
                  and not (n.startswith(('@rpath/', '@executable_path/', '@loader_path/'))
                           and os.path.exists(os.path.join(here, n.split('/', 1)[1]))))


def copy_game(p, bin_dir, game, files, exe):
    """kit/game: the front end's files from bin_dir (those files matches, only this game's executable),
    Vita3K's data, game.json and the playable folder's README."""
    os.makedirs(game)
    if not os.path.exists(os.path.join(bin_dir, exe)):
        sys.exit(f'no {exe} in {bin_dir}')
    suffix = os.path.splitext(exe)[1]
    for f in os.listdir(bin_dir):
        if os.path.isfile(os.path.join(bin_dir, f)) and files.match(f) and (not f.endswith(suffix) or f == exe):
            shutil.copy2(os.path.join(bin_dir, f), game)
    for d in BIN_DIRS:
        if os.path.isdir(os.path.join(bin_dir, d)):
            shutil.copytree(os.path.join(bin_dir, d), os.path.join(game, d))
    shutil.copy2(os.path.join(p.root, 'game.json'), game)
    if p.dist_readme:
        os.makedirs(os.path.dirname(os.path.join(game, p.dist_readme)), exist_ok=True)
        shutil.copy2(os.path.join(p.root, p.dist_readme), os.path.join(game, p.dist_readme))


def build_macos(a, lock, p, out):
    """The macOS setup (Apple Silicon): python/ (python-build-standalone, Tk included), kit/,
    setup.command; a .tar.gz, which keeps the executables' modes and Python's symbolic links."""
    python_standalone(lock, out)
    copy_kit(out)
    game = os.path.join(out, 'kit', 'game')
    exe = p.id + '.arm64'
    copy_game(p, os.path.abspath(a.bin), game, MACOS_BIN_FILES, exe)
    if not os.path.exists(os.path.join(game, 'libMoltenVK.dylib')):
        shutil.rmtree(out)
        sys.exit(f'refused, no libMoltenVK.dylib beside {exe} in {a.bin}')
    # without the debug map (stripping drops the linker's ad-hoc signature, which Apple Silicon
    # needs to run anything: sign it again, ad hoc)
    subprocess.run(['strip', '-S', os.path.join(game, exe)], check=True)
    subprocess.run(['codesign', '--force', '--sign', '-', os.path.join(game, exe)], check=True)

    with open(os.path.join(out, 'README.txt'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'{p.name}: setup (macOS 13.3 or newer, Apple Silicon)\n\n'
                'Double-click setup.command. macOS first refuses it, as it does anything downloaded that\n'
                'isn\'t signed by a registered Apple developer: click Done, open System Settings > Privacy &\n'
                'Security, scroll down to "setup.command was blocked" and click Open Anyway (once). Or, in\n'
                'Terminal, run: sh <this folder>/setup.command\n\n'
                'The setup asks for your own dump of the game, downloads the PS Vita firmware from Sony and a\n'
                'compiler (zig, from ziglang.org), and builds the game into the folder you choose (by default\n'
                f'"{p.id}" next to this setup\'s folder). Play from the app in that folder, or from\n'
                'Applications if you let the setup put it there. Nothing in this folder is the game itself:\n'
                'once the setup has finished, this folder can be deleted.\n')
    launcher = os.path.join(out, 'setup.command')
    with open(launcher, 'w', encoding='ascii', newline='\n') as f:
        # A browser marks a download as quarantined, and unpacking passes the mark to every file:
        # Gatekeeper would then stop Python and each library it loads. The player let this script
        # through: it clears the mark from the rest of its folder. No arguments: the window; with
        # arguments, the console setup
        f.write('#!/bin/sh\nhere=$(cd "$(dirname "$0")" && pwd -P)\n'
                'xattr -dr com.apple.quarantine "$here" 2>/dev/null\n'
                'exec "$here/python/bin/python3" -I -u "$here/kit/tools/setup.py" "$@"\n')
    os.chmod(launcher, 0o755)

    missing = missing_macos_libs(os.path.join(game, exe))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {exe} needs libraries a player\'s Mac may lack: ' + ', '.join(missing))
    release_check(out)

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')
    if a.zip:
        archive = shutil.make_archive(out, 'gztar', os.path.dirname(out), os.path.basename(out))
        print(f'{archive}: {os.path.getsize(archive) / 2**20:,.0f} MB, SHA-256 {sha256(archive)}')


def build_linux(a, lock, p, out):
    """The Linux setup: python/ (python-build-standalone, Tk included), kit/, setup.sh; a .tar.gz,
    which keeps the executables' modes."""
    python_standalone(lock, out)
    copy_kit(out)

    game = os.path.join(out, 'kit', 'game')
    exe = p.id + '.x86_64'
    copy_game(p, os.path.abspath(a.bin), game, LINUX_BIN_FILES, exe)
    # without its debug information (most of a RelWithDebInfo build's size)
    objcopy = shutil.which('objcopy')
    if objcopy:
        subprocess.run([objcopy, '--strip-debug', os.path.join(game, exe)], check=True)
    else:
        print(f'warning: no objcopy: {exe} keeps its debug information')

    with open(os.path.join(out, 'README.txt'), 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'{p.name}: setup (Linux, x86-64; Steam Deck in desktop mode)\n\n'
                'Run setup.sh (from a file manager, or ./setup.sh in a terminal). It asks for your own dump of\n'
                'the game, downloads the PS Vita firmware from Sony and a compiler (zig, from ziglang.org),\n'
                'and builds the game into the folder you choose (by default\n'
                f'"{p.id}" next to this setup\'s folder). Play from that folder, or from the applications menu\n'
                'entry the setup can make; on a Steam Deck, add that entry to Steam with "Add a Non-Steam\n'
                'Game" to play in Game Mode. Nothing in this folder is the game itself: once the setup has\n'
                'finished, this folder can be deleted.\n')
    launcher = os.path.join(out, 'setup.sh')
    with open(launcher, 'w', encoding='ascii', newline='\n') as f:
        # no arguments: the window; otherwise the console setup
        f.write('#!/bin/sh\nhere=$(dirname "$(readlink -f "$0")")\n'
                'exec "$here/python/bin/python3" -I -u "$here/kit/tools/setup.py" "$@"\n')
    os.chmod(launcher, 0o755)

    missing = missing_libs(os.path.join(game, exe))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {exe} needs libraries a player\'s system may lack: ' + ', '.join(missing))
    release_check(out)

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')
    if a.zip:
        archive = shutil.make_archive(out, 'gztar', os.path.dirname(out), os.path.basename(out))
        print(f'{archive}: {os.path.getsize(archive) / 2**20:,.0f} MB, SHA-256 {sha256(archive)}')


def build(a):
    lock = json.load(open(a.lock, encoding='utf-8'))
    p = Project(a.project)
    if lock.get('platform') in ('linux', 'macos'):
        out = os.path.abspath(a.out)
        if os.path.exists(out):
            shutil.rmtree(out)
        os.makedirs(out)
        return (build_macos if lock['platform'] == 'macos' else build_linux)(a, lock, p, out)
    out = os.path.abspath(a.out)
    if os.path.exists(out):
        shutil.rmtree(out)
    os.makedirs(out)
    paths = fetch(lock, os.path.join(os.path.dirname(out), 'setup-downloads'))  # beside the setup

    # Python, embeddable, with the wheels in Lib/site-packages; its ._pth file is the whole sys.path
    py = os.path.join(out, 'python')
    with zipfile.ZipFile(paths['python'][0]) as z:
        z.extractall(py)
    site = os.path.join(py, 'Lib', 'site-packages')
    for w in paths['wheel']:
        with zipfile.ZipFile(w) as z:
            z.extractall(site)
    pth = glob.glob(os.path.join(py, 'python*._pth'))[0]
    zipname = os.path.basename(pth)[:-5] + '.zip'
    with open(pth, 'w', encoding='ascii', newline='\r\n') as f:
        f.write(f'{zipname}\n.\nLib\nLib\\site-packages\n')
    # Tk: an administrative install of the MSI (no elevation), then only what tkinter loads
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(['msiexec.exe', '/a', paths['tcltk'][0], '/qn', f'TARGETDIR={tmp}\\tk'])
        if r.returncode:
            sys.exit(f'unpacking tcltk.msi failed ({r.returncode})')
        tk = os.path.join(tmp, 'tk')
        for f in os.listdir(os.path.join(tk, 'DLLs')):
            shutil.copy2(os.path.join(tk, 'DLLs', f), py)
        shutil.copytree(os.path.join(tk, 'Lib', 'tkinter'), os.path.join(py, 'Lib', 'tkinter'),
                        ignore=shutil.ignore_patterns('test', '__pycache__'))
        for d in ('tcl8', 'tcl8.6', 'tk8.6'):
            shutil.copytree(os.path.join(tk, 'tcl', d), os.path.join(py, 'tcl', d),
                            ignore=shutil.ignore_patterns('demos', 'images', 'tzdata'))

    # the kit: recompiler and tools
    copy_kit(out)

    # the game's front end and its description
    game = os.path.join(out, 'kit', 'game')
    os.makedirs(game)
    bin_dir = os.path.abspath(a.bin)
    if not os.path.exists(os.path.join(bin_dir, p.id + '.exe')):
        sys.exit(f'no {p.id}.exe in {bin_dir}')
    for f in os.listdir(bin_dir):
        if os.path.isfile(os.path.join(bin_dir, f)) and BIN_FILES.match(f) \
                and (not f.endswith('.exe') or f == p.id + '.exe'):
            shutil.copy2(os.path.join(bin_dir, f), game)
    for d in BIN_DIRS:
        if os.path.isdir(os.path.join(bin_dir, d)):
            shutil.copytree(os.path.join(bin_dir, d), os.path.join(game, d))
    shutil.copy2(os.path.join(p.root, 'game.json'), game)
    if p.dist_readme:
        os.makedirs(os.path.dirname(os.path.join(game, p.dist_readme)), exist_ok=True)
        shutil.copy2(os.path.join(p.root, p.dist_readme), os.path.join(game, p.dist_readme))

    with open(os.path.join(out, 'README.txt'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(f'{p.name}: setup\n\n'
                'Run setup.bat. It asks for your own dump of the game, downloads the PS Vita firmware from\n'
                'Sony and a compiler, and builds the game into the folder you choose (by default\n'
                f'"{p.id}" next to this setup\'s folder). Play from that folder, or from the desktop\n'
                'shortcut the setup can make. Nothing in this folder is the game itself: once the setup\n'
                'has finished, this folder can be deleted.\n')

    with open(os.path.join(out, 'setup.bat'), 'w', encoding='ascii', newline='\r\n') as f:
        # no arguments: the window, without a console; otherwise the console setup
        f.write('@echo off\nif "%~1"=="" (\n  start "" "%~dp0python\\pythonw.exe" -I "%~dp0kit\\tools\\setup.py"\n'
                '  exit /b 0\n)\n"%~dp0python\\python.exe" -I -u "%~dp0kit\\tools\\setup.py" %*\n'
                'set rc=%errorlevel%\nif "%~1"=="--console" pause\nexit /b %rc%\n')

    # release check: the executable finds every DLL it imports on a player's PC
    missing = missing_dlls(os.path.join(game, p.id + '.exe'))
    if missing:
        shutil.rmtree(out)
        sys.exit(f'refused, {p.id}.exe imports DLLs the setup does not carry: ' + ', '.join(missing))

    # release check: nothing that looks like game or firmware data
    release_check(out)

    size = sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(out) for f in fs)
    print(f'{out}: {size / 2**20:,.0f} MB')
    if a.zip:
        archive = shutil.make_archive(out, 'zip', os.path.dirname(out), os.path.basename(out))
        print(f'{archive}: {os.path.getsize(archive) / 2**20:,.0f} MB, SHA-256 {sha256(archive)}')


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == 'resolve':
        resolve(sys.argv[2], os.path.join(os.getcwd(), 'build', 'setup-downloads'))
        return
    if len(sys.argv) >= 4 and sys.argv[1] in ('resolve-linux', 'resolve-macos'):
        resolve_linux(sys.argv[2], os.path.join(os.getcwd(), 'build', 'setup-downloads'), sys.argv[3],
                      sys.argv[1].split('-')[1])
        return
    ap = argparse.ArgumentParser(usage=__doc__)
    ap.add_argument('command', choices=['build'])
    ap.add_argument('lock')
    ap.add_argument('--project', default=os.getcwd())
    ap.add_argument('--bin', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--zip', action='store_true')
    build(ap.parse_args())


if __name__ == '__main__':
    main()
