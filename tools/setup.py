"""The player's setup (make-setup.py lays it out; setup.bat, setup.sh or setup.command runs it): from
the player's own dump of the game to a playable folder. It downloads the PS Vita firmware from Sony
and a compiler for the recompiled modules (Windows: from LLVM's and Microsoft's servers; Linux and
macOS: zig, from ziglang.org), installs the game, recompiles its code and compiles it on this
computer. Nothing of the game is ever uploaded or shared.

usage (no arguments: a window asks, setupgui.py; --console: questions in the console):
  setup.bat|setup.sh|setup.command [--console]
  setup.bat|setup.sh|setup.command [--install <pkg|folder|zip|vpk> [--license <work.bin|rif> | --zrif Z]] ...
            [--out DIR] [--cache DIR] [--accept-microsoft-license] [--jobs N] [--shortcut] [--steam]

  --install   repeat for each part, in order: the game, then its update, then add-ons
  --out       the playable folder (default: <game id> next to the setup)
  --cache     the downloads, shared by every game's setup (default %LOCALAPPDATA%\\vita-recomps; Linux:
              ~/.cache/vita-recomps; macOS: ~/Library/Caches/vita-recomps)
  --accept-microsoft-license   Windows only: the compiler's C++ runtime and SDK
  --shortcut  Windows: a shortcut on the desktop. Linux: an entry in the applications menu (which
              Steam's "Add a Non-Steam Game" lists) and on the desktop. macOS: the game's app in
              ~/Applications too (Launchpad and Spotlight list it)
  --steam     Linux: add the game to Steam as a non-Steam game (Steam must be running), as SteamOS's
              "Add to Steam" does; on a Steam Deck it then starts from Game Mode
"""
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

TOOLS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOLS)
import toolchain  # noqa: E402
from gameproject import EXE_SUFFIX, MACOS, WINDOWS  # noqa: E402

# the minimum macOS of the fork's build (CMAKE_OSX_DEPLOYMENT_TARGET) and of the modules (toolchain.py)
MACOS_MINIMUM = '13.3'

SETUP = os.path.dirname(os.path.dirname(TOOLS))
GAME = os.path.join(SETUP, 'kit', 'game')  # the template the playable folder is made from


def desktop_shortcut(exe, name):
    """A shortcut to exe on the player's desktop (OneDrive's too); its path."""
    env = dict(os.environ, SHORTCUT_TARGET=exe, SHORTCUT_NAME=name)
    script = ("$d = [Environment]::GetFolderPath('Desktop'); "
              "$l = Join-Path $d ($env:SHORTCUT_NAME + '.lnk'); "
              "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($l); "
              "$s.TargetPath = $env:SHORTCUT_TARGET; "
              "$s.WorkingDirectory = Split-Path $env:SHORTCUT_TARGET; $s.Save(); $l")
    r = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script], env=env,
                       capture_output=True, text=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return r.stdout.strip() if r.returncode == 0 else ''


def desktop_entry(exe, game, out, menu):
    """Linux: <id>.desktop in the playable folder, and with menu in the applications menu and on the
    desktop too; the folder's file and the menu entry's path ('' when not made)."""
    icon = os.path.join(out, game['id'] + '.png')
    text = ('[Desktop Entry]\nType=Application\n'
            f'Name={game["name"]}\nExec="{exe}"\nPath={out}\nTerminal=false\nCategories=Game;\n'
            + (f'Icon={icon}\n' if os.path.exists(icon) else ''))
    home = os.path.expanduser('~')
    data = os.environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
    folders = [out] + ([os.path.join(data, 'applications'), os.path.join(home, 'Desktop')] if menu else [])
    made = []
    for folder in folders:
        if not os.path.isdir(os.path.dirname(folder)):
            continue
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, game['id'] + '.desktop')
        with open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
        os.chmod(path, 0o755)  # KDE starts a desktop file from the desktop only when it is executable
        made.append(path)
    return made[0], (made[1] if len(made) > 1 else '')


def add_to_steam(desktop):
    """Linux: hand a .desktop file to the running Steam client as a non-Steam game, as SteamOS's "Add
    to Steam" (holo-add-to-steam) does. Steam takes steam://addnonsteamgame/<path> only while
    /tmp/addnonsteamgamefile exists, so that a link elsewhere can't add games. Returns what happened."""
    steam = shutil.which('steam')
    running = subprocess.run(['pgrep', '-x', 'steam'], capture_output=True).returncode == 0
    if not steam or not running:
        return (f'Steam is not running: start it, then add {desktop} with "Add a Non-Steam Game" '
                '(or run the setup again with --steam)')
    open('/tmp/addnonsteamgamefile', 'a').close()
    url = 'steam://addnonsteamgame/' + urllib.parse.quote(desktop, safe='')
    try:
        # the steam script passes the link to the running client and returns
        subprocess.run([steam, url], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass
    return f'added to Steam as a non-Steam game ({desktop})'


def app_name(game):
    """The game's name as a file name: Finder shows ':' as '/', so the subtitle follows a dash."""
    return game['name'].replace(': ', ' - ').replace(':', '-').replace('/', '-')


# Apple's app icon grid: a 1024-point canvas, the artwork an 824-point rounded square in its middle with a
# slight shadow below. macOS doesn't shape an app's icon itself: a square picture fills the whole canvas
# and looks bigger than the other icons in the Dock, with sharp corners. Drawn with AppKit through
# osascript's JavaScript bridge, so the setup still needs nothing but macOS's own python3.
MAC_ICON_SCRIPT = '''
ObjC.import('AppKit');
function run(argv) {
    const src = $.NSImage.alloc.initWithContentsOfFile(argv[0]);
    if (src.isNil()) return 'unreadable';
    const size = 1024, side = 824, radius = 185.4, inset = (size - side) / 2;
    const rep = $.NSBitmapImageRep.alloc.initWithBitmapDataPlanesPixelsWidePixelsHighBitsPerSampleSamplesPerPixelHasAlphaIsPlanarColorSpaceNameBytesPerRowBitsPerPixel(
        null, size, size, 8, 4, true, false, $.NSDeviceRGBColorSpace, 0, 0);
    // (setters called as methods: assigning a property in JXA doesn't reach Objective-C)
    const context = $.NSGraphicsContext.graphicsContextWithBitmapImageRep(rep);
    $.NSGraphicsContext.saveGraphicsState;
    $.NSGraphicsContext.setCurrentContext(context);
    context.setImageInterpolation($.NSImageInterpolationHigh);
    const rect = $.NSMakeRect(inset, inset + 6, side, side);
    const shape = $.NSBezierPath.bezierPathWithRoundedRectXRadiusYRadius(rect, radius, radius);
    const shadow = $.NSShadow.alloc.init;
    shadow.setShadowOffset($.NSMakeSize(0, -10));
    shadow.setShadowBlurRadius(24);
    shadow.setShadowColor($.NSColor.colorWithCalibratedWhiteAlpha(0, 0.3));
    $.NSGraphicsContext.saveGraphicsState;
    shadow.set;
    $.NSColor.blackColor.setFill;
    shape.fill;
    $.NSGraphicsContext.restoreGraphicsState;
    shape.addClip;
    // only the opaque part: a game icon may have its own transparent margin and corners (the middle
    // row and column give the margins; colorAtXY counts y from the top, the image from the bottom)
    const pixels = $.NSBitmapImageRep.imageRepWithData(src.TIFFRepresentation);
    const w = pixels.pixelsWide, h = pixels.pixelsHigh;
    const opaque = (x, y) => pixels.colorAtXY(x, y).alphaComponent > 0.5;
    const edge = (n, test) => { let i = 0; while (i < n / 2 && !test(i)) i++; return i < n / 2 ? i : 0; };
    const left = edge(w, x => opaque(x, h >> 1)), right = edge(w, x => opaque(w - 1 - x, h >> 1));
    const top = edge(h, y => opaque(w >> 1, y)), bottom = edge(h, y => opaque(w >> 1, h - 1 - y));
    const sx = src.size.width / w, sy = src.size.height / h;
    const from = $.NSMakeRect(left * sx, bottom * sy, (w - left - right) * sx, (h - top - bottom) * sy);
    src.drawInRectFromRectOperationFraction(rect, from, $.NSCompositingOperationSourceOver, 1.0);
    $.NSGraphicsContext.restoreGraphicsState;
    const data = rep.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
    return data.writeToFileAtomically(argv[1], true) ? 'ok' : 'unwritable';
}
'''


def mac_icon_shape(png, out):
    """png drawn into the macOS icon shape (see MAC_ICON_SCRIPT) as out; False if that failed."""
    r = subprocess.run(['osascript', '-l', 'JavaScript', '-e', MAC_ICON_SCRIPT, png, out],
                       capture_output=True, text=True)
    return r.returncode == 0 and r.stdout.strip() == 'ok' and os.path.exists(out)


def mac_icns(png, icns):
    """png as icns, shaped as a macOS icon, every size from 16 to 1024 (512@2x) through an iconset
    (sips and iconutil, part of macOS; sips alone writes an .icns only up to 512). Falls back to sips
    on the unshaped picture."""
    with tempfile.TemporaryDirectory() as tmp:
        shaped = os.path.join(tmp, 'icon.png')
        source = shaped if mac_icon_shape(png, shaped) else png
        iconset = os.path.join(tmp, 'icon.iconset')
        os.makedirs(iconset)
        ok = True
        for size in (16, 32, 128, 256, 512):
            for scale, suffix in ((1, ''), (2, '@2x')):
                name = os.path.join(iconset, f'icon_{size}x{size}{suffix}.png')
                ok = ok and subprocess.run(['sips', '-z', str(size * scale), str(size * scale), source,
                                            '--out', name], capture_output=True).returncode == 0
        if ok and subprocess.run(['iconutil', '-c', 'icns', iconset, '-o', icns],
                                 capture_output=True).returncode == 0 and os.path.exists(icns):
            return True
    return subprocess.run(['sips', '-s', 'format', 'icns', png, '--out', icns],
                          capture_output=True).returncode == 0


def mac_app(exe, game, out, applications):
    """macOS: '<name>.app' in the playable folder, the game's launcher in Finder, the Dock and
    Spotlight, with its icon; with applications a copy in ~/Applications (Launchpad). The app holds
    only a script that starts exe, so the settings, logs and saves stay in the playable folder, and
    it is made here on the player's Mac (never downloaded, so Gatekeeper doesn't check it). Returns
    the folder's app and the copy ('' when not made)."""
    app = os.path.join(out, app_name(game) + '.app')
    shutil.rmtree(app, ignore_errors=True)
    os.makedirs(os.path.join(app, 'Contents', 'MacOS'))
    os.makedirs(os.path.join(app, 'Contents', 'Resources'))
    launcher = os.path.join(app, 'Contents', 'MacOS', game['id'])
    with open(launcher, 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'#!/bin/sh\ncd {shlex.quote(out)} && exec {shlex.quote(exe)} "$@"\n')
    os.chmod(launcher, 0o755)
    info = {
        'CFBundleName': game['name'], 'CFBundleDisplayName': game['name'],
        'CFBundleIdentifier': f'io.github.vita-recomps.{game["id"]}', 'CFBundleExecutable': game['id'],
        'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '1.0', 'CFBundleVersion': '1',
        'CFBundleInfoDictionaryVersion': '6.0', 'LSMinimumSystemVersion': MACOS_MINIMUM,
        'LSApplicationCategoryType': 'public.app-category.games', 'NSHighResolutionCapable': True,
    }
    # the icon: make-dist.py's PNG in the shape of a macOS icon, as an .icns (sips, part of macOS)
    png = os.path.join(out, game['id'] + '.png')
    icns = os.path.join(app, 'Contents', 'Resources', game['id'] + '.icns')
    if os.path.exists(png) and mac_icns(png, icns):
        info['CFBundleIconFile'] = game['id']
    with open(os.path.join(app, 'Contents', 'Info.plist'), 'wb') as f:
        plistlib.dump(info, f)
    copy = ''
    if applications:
        folder = os.path.join(os.path.expanduser('~'), 'Applications')
        os.makedirs(folder, exist_ok=True)
        copy = os.path.join(folder, os.path.basename(app))
        shutil.rmtree(copy, ignore_errors=True)
        shutil.copytree(app, copy, symlinks=True)
    return app, copy


def default_cache():
    """Where the downloads go, shared by every game's setup."""
    if WINDOWS:
        return os.path.join(os.environ.get('LOCALAPPDATA', SETUP), 'vita-recomps')
    if MACOS:
        return os.path.join(os.path.expanduser('~'), 'Library', 'Caches', 'vita-recomps')
    base = os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'vita-recomps')


def ask(prompt, default=''):
    answer = input(f'{prompt}{f" [{default}]" if default else ""}: ').strip().strip('"')
    return answer or default


def ask_parts(game):
    """The dump's parts, asked one by one: the game, then its update and add-ons."""
    parts = []
    print(f'\nWhere is your dump of {game["name"]} ({" or ".join([game["title_id"]] + game.get("other_title_ids", []))})? A pkg, a folder, a zip or a vpk.')
    labels = ['the game', 'its update (1.0x patch)', 'an add-on (DLC)']
    while True:
        label = labels[min(len([p for p in parts if p == '--install']), 2)]
        path = ask(f'Path to {label}' + (' (empty: none)' if parts else ''))
        if not path:
            if parts:
                return parts
            continue
        if not os.path.exists(path):
            print(f'  {path} does not exist')
            continue
        parts += ['--install', os.path.abspath(path)]
        if path.lower().endswith('.pkg'):
            lic = ask('  its licence: work.bin or .rif path, or a zRIF (empty: covered by the game\'s)')
            if lic:
                parts += (['--license', os.path.abspath(lic)] if os.path.exists(lic) else ['--zrif', lic])


def main():
    argv = sys.argv[1:]
    with open(os.path.join(GAME, 'game.json'), encoding='utf-8') as f:
        game = json.load(f)
    default_out = os.path.join(os.path.dirname(SETUP), game['id'])
    platform = 'windows' if WINDOWS else 'macos' if MACOS else 'linux'
    lock_path = os.path.join(TOOLS, f'toolchain-{platform}.json')
    with open(lock_path, encoding='utf-8') as f:
        lock = json.load(f)
    if not argv:
        import setupgui
        setupgui.Window(game, os.path.abspath(__file__), default_out, lock.get('microsoft_license')).run()
        return
    if argv == ['--console']:
        argv = []
    parts, rest, i = [], [], 0
    while i < len(argv):
        if argv[i] in ('--install', '--license', '--zrif') and i + 1 < len(argv):
            parts += [argv[i], argv[i + 1]]
            i += 2
        else:
            rest.append(argv[i])
            i += 1
    opts = {}
    j = 0
    while j < len(rest):
        if rest[j] in ('--out', '--cache', '--jobs') and j + 1 < len(rest):
            opts[rest[j]] = rest[j + 1]
            j += 2
        elif rest[j] in ('--accept-microsoft-license', '--shortcut', '--steam'):
            opts[rest[j]] = True
            j += 1
        else:
            sys.exit(__doc__)
    interactive = not parts

    print(f'{game["name"]}: setup')
    if interactive:
        parts = ask_parts(game)
    out = opts.get('--out') or (ask('\nPlayable folder', default_out) if interactive else default_out)
    cache = opts.get('--cache') or default_cache()
    jobs = opts.get('--jobs') or str(max(1, min(os.cpu_count() or 4, 6)))

    # nothing to accept on Linux and macOS: zig is MIT, its LLVM parts Apache
    accepted = bool(opts.get('--accept-microsoft-license')) or 'microsoft_license' not in lock
    if not accepted:
        print('\nThe game\'s code is compiled on this PC with LLVM\'s clang and Microsoft\'s C++ runtime and\n'
              'Windows SDK, downloaded from Microsoft under Microsoft\'s licence (Visual Studio Build Tools):\n'
              f'  {lock["microsoft_license"]}')
        if not interactive or ask('Do you accept it? (yes/no)').lower() not in ('y', 'yes'):
            sys.exit('Not accepted: nothing was downloaded.')

    print(f'\n1/3 firmware (from Sony) and compiler ({lock["download_bytes"] / 2**20:,.0f} MB), into {cache}')
    py = [sys.executable, '-I', '-u']
    if subprocess.run(py + [os.path.join(TOOLS, 'fetch-firmware.py'), os.path.join(cache, 'firmware')]).returncode:
        sys.exit('downloading the firmware failed')
    tc_dir = os.path.join(cache, 'toolchain' if WINDOWS else f'toolchain-{platform}')
    pinned = [k for k in ('msvc', 'llvm_mingw', 'zig') if k in lock]  # the versions the lock file pins
    have = {}
    if os.path.exists(os.path.join(tc_dir, 'toolchain.json')):
        with open(os.path.join(tc_dir, 'toolchain.json'), encoding='utf-8') as f:
            have = json.load(f)
    if any(have.get(k) != lock[k] for k in pinned):
        toolchain.fetch(lock_path, tc_dir, True)

    print(f'\n2/3 installing the game, recompiling and compiling its code into {out} (several minutes)')
    firmware = [os.path.join(cache, 'firmware', 'PSVUPDAT.PUP'), os.path.join(cache, 'firmware', 'PSP2UPDAT.PUP')]
    r = subprocess.run(py + [os.path.join(TOOLS, 'install-game.py'), '--project', GAME, '--bin', GAME,
                             '--toolchain', tc_dir, '--out', out, '--jobs', jobs, '--firmware', *firmware] + parts)
    if r.returncode:
        sys.exit('the setup failed (see above)')

    exe = os.path.join(out, game['id'] + EXE_SUFFIX)
    if opts.get('--shortcut') and WINDOWS:
        lnk = desktop_shortcut(exe, game['name'])
        print(f'desktop shortcut: {lnk}' if lnk else 'the desktop shortcut could not be made')
    elif MACOS:
        app, copy = mac_app(exe, game, out, bool(opts.get('--shortcut')))
        print(f'app: {app}' + (f' (and in Applications: {copy})' if copy else ''))
        exe = app
    elif not WINDOWS:
        desktop, entry = desktop_entry(exe, game, out, bool(opts.get('--shortcut')))
        if opts.get('--shortcut'):
            print(f'applications menu entry: {entry}' if entry else 'the menu entry could not be made')
        if opts.get('--steam'):
            print(add_to_steam(desktop))
    print(f'\n3/3 done: the game is in {out}. Start {exe}')


if __name__ == '__main__':
    main()
