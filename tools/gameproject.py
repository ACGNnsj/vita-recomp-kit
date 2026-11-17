"""A game project's description (game.json at its root), for the install and packaging scripts.

game.json:
  {
    "id": "shinobido2",                       front end: <id>.exe (Linux: <id>.x86_64), <id>.ini, <id>/
    "name": "Shinobido 2: Revenge of Zen",
    "title_id": "PCSE00015",
    "other_title_ids": ["PCSB00123"],         the same game in other regions (optional): the
                                              installed one is used; title_id names the game
    "eboot_sha256": {"<sha256>": "1.02"},     the eboot.bin builds the port supports
    "modules": {"eboot": "eboot", "libc": "libc", "sysmodule": "fw_sysmodule"},
                                              module as dumped -> generated/<name> (recompiled)
    "load_bases": {"adhoc_matching": "80757000"},   modules recompiled for another address
    "dump_seconds": 45,                       how long --dump-elfs runs the game
    "dist_readme": "docs/dist-readme.txt",    README.txt of the packaged folder (project path)
    "dist_readme_linux": "docs/dist-readme-linux.txt",   the same on Linux (default: dist_readme)
    "dist_files": {"run-player2.bat": "..."}  more files written into the packaged folder (Windows)
    "dist_files_linux": {"run-player2.sh": "..."}   the same on Linux (written executable)
  }
"""
import json
import os
import sys

WINDOWS = sys.platform == 'win32'
EXE_SUFFIX = '.exe' if WINDOWS else '.x86_64'  # the fork's add_game names it <id>.x86_64 on Linux
MODULE_SUFFIX = '.dll' if WINDOWS else '.so'   # ssd_aot_<module>.dll / .so


class Project:
    def __init__(self, root):
        self.root = os.path.abspath(root)
        path = os.path.join(self.root, 'game.json')
        if not os.path.exists(path):
            sys.exit(f'no game.json in {self.root} (pass --project)')
        with open(path, encoding='utf-8') as f:
            g = json.load(f)
        self.id = g['id']
        self.name = g['name']
        self.title_id = g['title_id']
        self.title_ids = [self.title_id] + [t for t in g.get('other_title_ids', []) if t != self.title_id]
        self.eboot_sha256 = {k.lower(): v for k, v in g.get('eboot_sha256', {}).items()}
        self.modules = g.get('modules', {})
        self.load_bases = g.get('load_bases', {})
        self.dump_seconds = g.get('dump_seconds', 45)
        self.dist_readme = g.get('dist_readme') if WINDOWS else g.get('dist_readme_linux', g.get('dist_readme'))
        self.dist_files = g.get('dist_files', {}) if WINDOWS else g.get('dist_files_linux', {})
        self.exe_name = self.id + EXE_SUFFIX

    def installed_title(self, fs):
        """The title ID of the game installed in a Vita file system (ux0/app/<id>/eboot.bin), or None."""
        for t in self.title_ids:
            if os.path.exists(os.path.join(fs, 'ux0', 'app', t, 'eboot.bin')):
                return t
        return None

    def bin_dir(self, no_jit=False, config='RelWithDebInfo'):
        return os.path.join(self.root, 'build', 'vita3k-nojit' if no_jit else 'vita3k', 'bin', config)

    def exe(self, no_jit=False, bin_dir=None):
        """The front end: built in the project (build-vita3k.py), or in bin_dir (a prebuilt release)."""
        path = os.path.join(bin_dir or self.bin_dir(no_jit), self.exe_name)
        if not os.path.exists(path):
            sys.exit(f'no {path}: build {self.exe_name} first (tools/build-vita3k.py --project {self.root})')
        return os.path.abspath(path)
