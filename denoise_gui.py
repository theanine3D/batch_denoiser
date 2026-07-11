# Batch Denoise - denoise folders of images using Blender's compositor
# Denoise node (OpenImageDenoise) via headless Blender.
#
# Run with a normal Python 3 installation (stdlib only):
#   python denoise_gui.py

import codecs
import ctypes
import glob
import json
import os
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText

if sys.platform == 'win32':
    import winreg

# When frozen (PyInstaller), bundled data files live under sys._MEIPASS,
# not next to this script - __file__ doesn't point at real bundle contents.
if getattr(sys, 'frozen', False):
    APP_DIR = sys._MEIPASS
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
WORKER_SCRIPT = os.path.join(APP_DIR, 'blender_denoise.py')
PROTOCOL_PREFIX = '##J '

# Known-folder GUIDs that appear in Windows UserAssist entries.
KNOWN_FOLDER_GUIDS = {
    '{905E63B6-C1BF-494E-B29C-65B732D3D21A}':
        os.environ.get('ProgramFiles', r'C:\Program Files'),
    '{6D809377-6AF0-444B-8957-A3773F02200E}':
        os.environ.get('ProgramFiles', r'C:\Program Files'),
    '{7C5A40EF-A0FB-4BCF-874A-C0F2E0B9FA8E}':
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
    '{F1B32785-6FBA-4FCF-9D55-7B8E7F157091}':
        os.environ.get('LocalAppData', ''),
}


def _config_dir():
    """A user-writable directory, even when the script itself lives
    somewhere read-only (Program Files, /Applications, /usr/share, ...)."""
    if sys.platform == 'win32':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
        return os.path.join(base, 'BatchDenoise')
    if sys.platform == 'darwin':
        return os.path.expanduser('~/Library/Application Support/BatchDenoise')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'BatchDenoise')


CONFIG_PATH = os.path.join(_config_dir(), 'config.json')


# ---------------------------------------------------------------- detection

def _resolve_app_bundle(app_path):
    """macOS .app bundle -> its internal executable, read from Info.plist
    (falls back to the bundle's own name if the plist is missing/odd)."""
    exe_name = None
    try:
        with open(os.path.join(app_path, 'Contents', 'Info.plist'), 'rb') as fh:
            exe_name = plistlib.load(fh).get('CFBundleExecutable')
    except Exception:
        pass
    if not exe_name:
        exe_name = os.path.splitext(os.path.basename(app_path))[0]
    candidate = os.path.join(app_path, 'Contents', 'MacOS', exe_name)
    return candidate if os.path.isfile(candidate) else None


def _normalize_exe(path):
    """Validate a candidate path into a directly-executable Blender binary."""
    if not path:
        return None
    path = os.path.normpath(path.strip('"'))

    if sys.platform == 'win32':
        # Swap the launcher for the real binary: blender-launcher.exe
        # detaches from the console, which breaks stdout capture.
        base = os.path.basename(path).lower()
        if base == 'blender-launcher.exe':
            sibling = os.path.join(os.path.dirname(path), 'blender.exe')
            return sibling if os.path.isfile(sibling) else None
        if base == 'blender.exe' and os.path.isfile(path):
            return path
        return None

    if sys.platform == 'darwin':
        if path.lower().endswith('.app'):
            path = _resolve_app_bundle(path)
            if not path:
                return None
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None

    # Linux and other POSIX platforms: a real executable, snap/flatpak
    # export wrapper, or AppImage all satisfy this check directly.
    return path if os.path.isfile(path) and os.access(path, os.X_OK) else None


def _scan_program_files():
    hits = []
    roots = {os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')}
    for pf in filter(None, roots):
        base = os.path.join(pf, 'Blender Foundation')
        if not os.path.isdir(base):
            continue
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            m = re.match(r'Blender (\d+)\.(\d+)', name)
            if not m:
                continue
            exe = os.path.join(base, name, 'blender.exe')
            if os.path.isfile(exe):
                hits.append(((int(m.group(1)), int(m.group(2))), exe))
    hits.sort(reverse=True)
    return [exe for _ver, exe in hits]


def _scan_registry_association():
    """Read the .blend file association set by the Blender installer."""
    keys = [
        (winreg.HKEY_CURRENT_USER, r'Software\Classes\blendfile\shell\open\command'),
        (winreg.HKEY_LOCAL_MACHINE, r'Software\Classes\blendfile\shell\open\command'),
        (winreg.HKEY_CLASSES_ROOT, r'blendfile\shell\open\command'),
    ]
    results = []
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                command = winreg.QueryValue(key, None)
        except OSError:
            continue
        m = re.match(r'"([^"]+)"', command or '')
        if m:
            results.append(m.group(1))
    return results


def _scan_path():
    exe = shutil.which('blender')
    return [exe] if exe else []


def _scan_steam():
    results = []
    for pf in filter(None, {os.environ.get('ProgramFiles(x86)'),
                            os.environ.get('ProgramFiles')}):
        exe = os.path.join(pf, 'Steam', 'steamapps', 'common', 'Blender',
                           'blender.exe')
        if os.path.isfile(exe):
            results.append(exe)
    return results


def _scan_userassist():
    """Windows tracks launched programs in UserAssist (ROT13-encoded names)."""
    results = []
    base = r'Software\Microsoft\Windows\CurrentVersion\Explorer\UserAssist'
    try:
        ua = winreg.OpenKey(winreg.HKEY_CURRENT_USER, base)
    except OSError:
        return results
    with ua:
        i = 0
        while True:
            try:
                guid = winreg.EnumKey(ua, i)
            except OSError:
                break
            i += 1
            try:
                count_key = winreg.OpenKey(ua, guid + r'\Count')
            except OSError:
                continue
            with count_key:
                j = 0
                while True:
                    try:
                        name, _value, _vtype = winreg.EnumValue(count_key, j)
                    except OSError:
                        break
                    j += 1
                    decoded = codecs.decode(name, 'rot13')
                    low = decoded.lower()
                    if 'blender' not in low or not low.endswith('.exe'):
                        continue
                    for g, repl in KNOWN_FOLDER_GUIDS.items():
                        if repl and decoded.upper().startswith(g):
                            decoded = repl + decoded[len(g):]
                            break
                    results.append(decoded)
    return results


def _scan_common_dirs_macos():
    home = os.path.expanduser('~')
    patterns = [
        '/Applications/Blender*.app',
        os.path.join(home, 'Applications/Blender*.app'),
        '/opt/homebrew/Caskroom/blender/*/Blender.app',
        '/usr/local/Caskroom/blender/*/Blender.app',
    ]
    hits = []
    for pattern in patterns:
        hits.extend(sorted(glob.glob(pattern), reverse=True))
    return hits


def _scan_steam_macos():
    path = os.path.expanduser(
        '~/Library/Application Support/Steam/steamapps/common/Blender/Blender.app')
    return [path] if os.path.isdir(path) else []


def _scan_spotlight_macos():
    """Spotlight's index covers the whole filesystem, so it can find a
    Blender.app that lives outside /Applications - the macOS analogue of
    checking Windows' UserAssist "recently launched" list."""
    finder = shutil.which('mdfind')
    if not finder:
        return []
    try:
        out = subprocess.run([finder, '-name', 'Blender.app'],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _scan_common_dirs_linux():
    home = os.path.expanduser('~')
    simple = [
        '/usr/bin/blender',
        '/usr/local/bin/blender',
        '/snap/bin/blender',
        '/snap/blender/current/blender',
        os.path.join(home, '.local/share/flatpak/exports/bin/org.blender.Blender'),
        '/var/lib/flatpak/exports/bin/org.blender.Blender',
    ]
    hits = list(simple)
    patterns = [
        '/opt/blender*/blender',
        os.path.join(home, 'blender-*/blender'),
        os.path.join(home, '.local/blender-*/blender'),
        os.path.join(home, 'Applications/blender-*/blender'),
    ]
    for pattern in patterns:
        hits.extend(sorted(glob.glob(pattern), reverse=True))
    return hits


def _scan_steam_linux():
    home = os.path.expanduser('~')
    candidates = [
        os.path.join(home, '.local/share/Steam/steamapps/common/Blender/blender'),
        os.path.join(home, '.steam/steam/steamapps/common/Blender/blender'),
        os.path.join(home, '.var/app/com.valvesoftware.Steam/.local/share/'
                           'Steam/steamapps/common/Blender/blender'),
    ]
    return [p for p in candidates if os.path.isfile(p)]


def _scan_desktop_files_linux():
    """Parse .desktop launcher entries for an Exec= line mentioning Blender
    (catches AppImages and other installs that never land on PATH)."""
    home = os.path.expanduser('~')
    dirs = [
        os.path.join(home, '.local/share/applications'),
        '/usr/share/applications',
        '/usr/local/share/applications',
        '/var/lib/flatpak/exports/share/applications',
        os.path.join(home, '.local/share/flatpak/exports/share/applications'),
    ]
    results = []
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            if 'blender' not in name.lower() or not name.endswith('.desktop'):
                continue
            try:
                with open(os.path.join(d, name), 'r', encoding='utf-8',
                          errors='ignore') as fh:
                    text = fh.read()
            except OSError:
                continue
            m = re.search(r'^Exec=(.*)$', text, re.MULTILINE)
            if not m:
                continue
            token = m.group(1).split('%')[0].strip().split(' ')[0].strip('"')
            if token:
                results.append(token)
    return results


def _scan_locate_linux():
    """`locate`/`plocate` maintain a filesystem-wide index, similar in
    spirit to Spotlight on macOS - useful when nothing above finds it."""
    finder = shutil.which('plocate') or shutil.which('locate')
    if not finder:
        return []
    try:
        out = subprocess.run([finder, 'blender'], capture_output=True,
                             text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    results = []
    for line in out.splitlines():
        line = line.strip()
        low = line.lower()
        if low.endswith('/blender') or ('blender' in low and low.endswith('.appimage')):
            results.append(line)
    return results


def find_blender():
    """Return (exe_path, description of how it was found) or (None, None)."""
    if sys.platform == 'win32':
        strategies = [
            (_scan_program_files, 'found in Program Files'),
            (_scan_registry_association, 'found via .blend file association'),
            (_scan_path, 'found on PATH'),
            (_scan_steam, 'found in Steam library'),
            (_scan_userassist, 'found in recently launched programs'),
        ]
    elif sys.platform == 'darwin':
        strategies = [
            (_scan_common_dirs_macos, 'found in /Applications'),
            (_scan_path, 'found on PATH'),
            (_scan_steam_macos, 'found in Steam library'),
            (_scan_spotlight_macos, 'found via Spotlight search'),
        ]
    else:
        strategies = [
            (_scan_common_dirs_linux, 'found in a standard location'),
            (_scan_path, 'found on PATH'),
            (_scan_steam_linux, 'found in Steam library'),
            (_scan_desktop_files_linux, 'found via a desktop launcher entry'),
            (_scan_locate_linux, "found via the system's locate database"),
        ]
    for scan, how in strategies:
        try:
            candidates = scan()
        except Exception:
            continue
        for candidate in candidates:
            exe = _normalize_exe(candidate)
            if exe:
                return exe, how
    return None, None


def load_config():
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, 'w', encoding='utf-8') as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------------- GUI

class DenoiseApp:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.proc = None
        self.running = False
        self.cancel_requested = False
        self.total = 0
        self.done_counts = None
        self.tail = []  # last raw output lines, for diagnostics

        root.title('Batch Denoise (Blender)')
        root.minsize(560, 420)

        frame = ttk.Frame(root, padding=10)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(1, weight=1)

        # Blender path row
        ttk.Label(frame, text='Blender:').grid(row=0, column=0, sticky='w')
        self.blender_var = tk.StringVar()
        blender_entry = ttk.Entry(frame, textvariable=self.blender_var,
                                  state='readonly')
        blender_entry.grid(row=0, column=1, sticky='ew', padx=(6, 6))
        ttk.Button(frame, text='Change…', command=self.change_blender)\
            .grid(row=0, column=2)

        # Folder row
        ttk.Label(frame, text='Folder:').grid(row=1, column=0, sticky='w',
                                              pady=(8, 0))
        self.folder_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.folder_var)\
            .grid(row=1, column=1, sticky='ew', padx=(6, 6), pady=(8, 0))
        ttk.Button(frame, text='Browse…', command=self.browse_folder)\
            .grid(row=1, column=2, pady=(8, 0))

        # Recursive checkbox + buttons
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text='Recursive (include subfolders)',
                        variable=self.recursive_var)\
            .grid(row=2, column=1, sticky='w', padx=(6, 0), pady=(8, 0))

        button_row = ttk.Frame(frame)
        button_row.grid(row=3, column=0, columnspan=3, pady=(10, 0))
        self.start_btn = ttk.Button(button_row, text='Batch Denoise',
                                    command=self.start)
        self.start_btn.pack(side='left')
        self.cancel_btn = ttk.Button(button_row, text='Cancel',
                                     command=self.cancel, state='disabled')
        self.cancel_btn.pack(side='left', padx=(8, 0))

        # Progress
        self.progress = ttk.Progressbar(frame, mode='determinate')
        self.progress.grid(row=4, column=0, columnspan=3, sticky='ew',
                           pady=(12, 0))
        self.status_var = tk.StringVar(value='Ready.')
        ttk.Label(frame, textvariable=self.status_var)\
            .grid(row=5, column=0, columnspan=3, sticky='w', pady=(4, 0))

        # Log
        self.log_box = ScrolledText(frame, height=12, state='disabled',
                                    wrap='none', font='TkFixedFont')
        self.log_box.grid(row=6, column=0, columnspan=3, sticky='nsew',
                          pady=(8, 0))
        self.log_box.tag_configure('error', foreground='#c00000')
        frame.rowconfigure(6, weight=1)

        self.detect_blender()

    # ------------------------------------------------------------- logging

    def log(self, text, tag=None):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', text + '\n', tag or ())
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    # ------------------------------------------------------ blender config

    def detect_blender(self):
        cfg = load_config()
        saved = _normalize_exe(cfg.get('blender_path', ''))
        if saved:
            self.blender_var.set(saved)
            self.log('Using saved Blender: ' + saved)
            return
        exe, how = find_blender()
        if exe:
            self.blender_var.set(exe)
            self.log('Blender %s: %s' % (how, exe))
            save_config({'blender_path': exe})
        else:
            self.log('Blender was not found automatically. '
                     'Use "Change…" to locate your Blender installation.',
                     'error')

    def change_blender(self):
        if sys.platform == 'win32':
            path = filedialog.askopenfilename(
                title='Locate blender.exe',
                filetypes=[('Blender executable', 'blender.exe'),
                           ('Executables', '*.exe')])
        elif sys.platform == 'darwin':
            path = filedialog.askopenfilename(
                title='Locate Blender.app',
                initialdir='/Applications',
                filetypes=[('Applications', '*.app'), ('All files', '*')])
        else:
            path = filedialog.askopenfilename(
                title='Locate the blender executable',
                initialdir='/usr/bin')
        if not path:
            return
        exe = _normalize_exe(path)
        if not exe:
            messagebox.showwarning(
                'Invalid selection',
                'Please select a valid Blender executable'
                + (' (Blender.app)' if sys.platform == 'darwin' else '') + '.')
            return
        self.blender_var.set(exe)
        save_config({'blender_path': exe})
        self.log('Blender set to: ' + exe)

    def browse_folder(self):
        path = filedialog.askdirectory(title='Select folder with images')
        if path:
            self.folder_var.set(os.path.normpath(path))

    # -------------------------------------------------------------- run

    def start(self):
        blender = self.blender_var.get()
        folder = self.folder_var.get().strip()
        if not blender or not os.path.isfile(blender):
            messagebox.showwarning(
                'Blender not set',
                'Please locate your Blender installation first.')
            return
        if not folder or not os.path.isdir(folder):
            messagebox.showwarning('Invalid folder',
                                   'Please select an existing folder.')
            return
        if not os.path.isfile(WORKER_SCRIPT):
            messagebox.showerror(
                'Missing file',
                'blender_denoise.py was not found next to this script.')
            return

        self.running = True
        self.cancel_requested = False
        self.total = 0
        self.done_counts = None
        self.tail = []
        self.start_btn.configure(state='disabled')
        self.cancel_btn.configure(state='normal')
        self.progress.configure(mode='indeterminate', value=0)
        self.progress.start(12)
        self.status_var.set('Starting Blender…')
        self.log('Launching Blender (headless)…')

        recursive = self.recursive_var.get()
        thread = threading.Thread(
            target=self._worker, args=(blender, folder, recursive),
            daemon=True)
        thread.start()
        self.root.after(50, self._poll)

    def _worker(self, blender, folder, recursive):
        cmd = [blender, '-b', '--factory-startup', '-P', WORKER_SCRIPT,
               '--', folder, '1' if recursive else '0']
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                env=env)
        except OSError as exc:
            self.queue.put(('spawn_error', str(exc)))
            return
        self.proc = proc
        for raw in proc.stdout:
            line = raw.decode('utf-8', 'replace').rstrip()
            if line:
                self.queue.put(('line', line))
        self.queue.put(('exit', proc.wait()))

    def cancel(self):
        if self.proc and self.running:
            self.cancel_requested = True
            self.cancel_btn.configure(state='disabled')
            self.log('Cancelling…')
            try:
                self.proc.terminate()
            except OSError:
                pass

    # ------------------------------------------------------------- polling

    def _poll(self):
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == 'line':
                    self._handle_line(payload)
                elif kind == 'spawn_error':
                    self._finish_spawn_error(payload)
                    return
                elif kind == 'exit':
                    self._finish(payload)
                    return
        except queue.Empty:
            pass
        if self.running:
            self.root.after(50, self._poll)

    def _handle_line(self, line):
        if not line.startswith(PROTOCOL_PREFIX):
            self.tail.append(line)
            del self.tail[:-15]
            return
        try:
            msg = json.loads(line[len(PROTOCOL_PREFIX):])
        except ValueError:
            return
        mtype = msg.get('type')
        if mtype == 'total':
            self.total = msg['count']
            self.progress.stop()
            self.progress.configure(mode='determinate', maximum=max(self.total, 1),
                                    value=0)
            self.status_var.set('0 / %d' % self.total)
            self.log('Found %d image(s) to denoise.' % self.total)
        elif mtype == 'ok':
            self.progress.configure(value=msg['i'])
            self.status_var.set('%d / %d' % (msg['i'], self.total))
            self.log('Denoised: %s  ->  %s' % (msg['path'], msg['out']))
        elif mtype == 'err':
            self.progress.configure(value=msg['i'])
            self.status_var.set('%d / %d' % (msg['i'], self.total))
            self.log('ERROR: %s - %s' % (msg['path'], msg['msg']), 'error')
        elif mtype == 'fatal':
            self.log('FATAL: ' + msg.get('msg', 'unknown error'), 'error')
        elif mtype == 'done':
            self.done_counts = (msg['ok'], msg['err'])

    def _reset_controls(self):
        self.running = False
        self.proc = None
        self.progress.stop()
        self.progress.configure(mode='determinate')
        self.start_btn.configure(state='normal')
        self.cancel_btn.configure(state='disabled')

    def _finish_spawn_error(self, msg):
        self._reset_controls()
        self.status_var.set('Failed to launch Blender.')
        self.log('Failed to launch Blender: ' + msg, 'error')
        messagebox.showerror('Launch failed',
                             'Could not launch Blender:\n' + msg)

    def _finish(self, exit_code):
        self._reset_controls()
        if self.cancel_requested:
            self.status_var.set('Cancelled.')
            self.log('Batch denoise cancelled.')
            messagebox.showinfo('Cancelled', 'Batch denoise was cancelled.')
            return
        if self.done_counts is not None:
            ok, err = self.done_counts
            self.progress.configure(value=self.total)
            self.status_var.set('Done: %d denoised, %d error(s).' % (ok, err))
            self.log('Done: %d denoised, %d error(s).' % (ok, err))
            if ok == 0 and err == 0:
                messagebox.showinfo('Batch denoise',
                                    'No images were found in that folder.')
            elif err:
                messagebox.showwarning(
                    'Batch denoise complete',
                    'Denoised %d image(s).\n%d image(s) failed - see the log '
                    'for details.' % (ok, err))
            else:
                messagebox.showinfo('Batch denoise complete',
                                    'Successfully denoised %d image(s).' % ok)
        else:
            self.status_var.set('Blender exited unexpectedly.')
            self.log('Blender exited unexpectedly (code %s). Last output:'
                     % exit_code, 'error')
            for line in self.tail:
                self.log('  ' + line, 'error')
            messagebox.showerror(
                'Batch denoise failed',
                'Blender exited unexpectedly (code %s).\nSee the log for '
                'details.' % exit_code)


def main():
    if sys.platform == 'win32':
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    DenoiseApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
