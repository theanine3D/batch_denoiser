# Batch Denoiser (Blender)

<img width="676" height="444" alt="image" src="https://github.com/user-attachments/assets/cf4ec62f-7b63-48c1-af13-a305ddaa1a7f" />

A tool for fast batch denoising images using Blender's compositor Denoise node
(via OpenImageDenoise). A headless Blender process is launched once in the
background to handle the whole batch, so Blender's startup cost is paid only once.

## Usage

Run from source:

```
python denoise_gui.py
```

Or use the standalone build (see Releases) — just double-click the executable
(ie. `BatchDenoise.exe`), no Python installation required.

1. The app auto-detects Blender using platform-appropriate strategies:
   - **Windows**: Program Files → `.blend` file association (registry) →
     PATH → Steam → UserAssist "recently launched" registry.
   - **macOS**: `/Applications` (incl. Homebrew Caskroom) → PATH → Steam →
     Spotlight (`mdfind -name Blender.app`), which can find installs
     anywhere on disk.
   - **Linux**: standard paths (`/usr/bin`, `/opt`, snap, flatpak exports) →
     PATH → Steam → `.desktop` launcher entries → `locate`/`plocate`
     database.

   Use **Change…** if it picks the wrong one, or if nothing was found; the
   choice is saved to a config file in the OS's standard per-user config
   location (`%APPDATA%\BatchDenoiser`, `~/Library/Application Support/BatchDenoiser`,
   or `~/.config/BatchDenoiser`) so it works even if the app itself is
   installed somewhere read-only.
2. Pick a folder, optionally tick **Recursive**, press **Batch Denoiser**.
3. Results are written to a `denoised` subfolder next to each source image
   (existing outputs are overwritten; originals are never touched).

## Notes

- Supported formats: png, jpg/jpeg, tga, bmp, tif/tiff, webp, exr, hdr.
  Output matches the source format; 16-bit and float sources keep their depth.
- Folders named `denoised` are skipped when scanning, so re-runs never
  re-denoise previous output.
- Corrupt/unreadable images are logged as errors and the batch continues.
- Requires Blender 4.x/5.x.

## Files

- `denoise_gui.py` — the GUI (plain Python 3, stdlib only).
- `blender_denoiser.py` — worker script that runs inside headless Blender;
  can also be used standalone:
  `blender.exe -b --factory-startup -P blender_denoiser.py -- <folder> <0|1>`

## Building a standalone .exe (Windows)

Built with PyInstaller in **onedir** mode.

```
pip install pyinstaller
pyinstaller --onedir --windowed --name "BatchDenoiser" ^
    --add-data "blender_denoise.py;." --noconfirm denoise_gui.py
```

Output is `dist\BatchDenoiser\BatchDenoiser.exe` plus a `dist\BatchDenoiser\_internal`
folder — **both must ship together**; the exe alone won't run without
`_internal`. Zip the whole `dist\BatchDenoiser` folder when copying.
