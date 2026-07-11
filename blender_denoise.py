# Runs INSIDE Blender (headless). Do not run with plain Python.
#
# Usage:
#   blender.exe -b --factory-startup -P blender_denoise.py -- <folder> <0|1 recursive>
#
# Denoises every supported image found in <folder> using the compositor
# Denoise node (OpenImageDenoise) and saves results to a "denoised"
# subfolder next to each source image, overwriting existing outputs.
#
# Progress protocol: lines starting with "##J " followed by one JSON object.
#   {"type": "total", "count": N}
#   {"type": "ok",    "i": i, "path": ..., "out": ...}
#   {"type": "err",   "i": i, "path": ..., "msg": ...}
#   {"type": "done",  "ok": N, "err": N}
#   {"type": "fatal", "msg": ...}

import bpy
import json
import os
import sys
import traceback

IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.tga', '.bmp', '.tif', '.tiff',
              '.webp', '.exr', '.hdr'}
OUT_DIR_NAME = 'denoised'


def emit(**obj):
    sys.stdout.write('##J ' + json.dumps(obj) + '\n')
    sys.stdout.flush()


def find_images(root, recursive):
    found = []
    if recursive:
        for dirpath, dirnames, filenames in os.walk(root):
            # Never descend into output folders, so re-runs don't
            # re-denoise previous results.
            dirnames[:] = [d for d in dirnames if d.lower() != OUT_DIR_NAME]
            for name in sorted(filenames):
                if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                    found.append(os.path.join(dirpath, name))
    else:
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if (os.path.isfile(path)
                    and os.path.splitext(name)[1].lower() in IMAGE_EXTS):
                found.append(path)
    return found


def setup_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    # Workbench renders the (empty) 3D scene fastest; the compositor
    # output is what actually gets saved.
    try:
        scene.render.engine = 'BLENDER_WORKBENCH'
    except Exception:
        pass

    cam_data = bpy.data.cameras.new('denoise_cam')
    cam_obj = bpy.data.objects.new('denoise_cam', cam_data)
    scene.collection.objects.link(cam_obj)
    scene.camera = cam_obj

    # Preserve pixel values: no artistic view transform, no dithering.
    scene.display_settings.display_device = 'sRGB'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0.0
    scene.view_settings.gamma = 1.0
    scene.render.dither_intensity = 0.0

    scene.render.resolution_percentage = 100
    scene.render.use_compositing = True
    scene.render.use_sequencer = False
    try:
        scene.render.compositor_device = 'GPU'
    except Exception:
        pass

    # Blender 5.x: the compositor is a node group assigned to the scene.
    nt = bpy.data.node_groups.new('BatchDenoise', 'CompositorNodeTree')
    nt.interface.new_socket('Image', in_out='OUTPUT',
                            socket_type='NodeSocketColor')
    n_img = nt.nodes.new('CompositorNodeImage')
    n_dn = nt.nodes.new('CompositorNodeDenoise')
    n_out = nt.nodes.new('NodeGroupOutput')
    try:
        n_dn.prefilter = 'ACCURATE'
    except Exception:
        pass
    try:
        n_dn.quality = 'HIGH'
    except Exception:
        pass
    nt.links.new(n_img.outputs['Image'], n_dn.inputs['Image'])
    nt.links.new(n_dn.outputs['Image'], n_out.inputs['Image'])
    scene.compositing_node_group = nt

    return scene, n_img


def output_settings(img, ext):
    """Pick an output format matching the source file's format."""
    has_alpha = img.depth in (32, 64, 128)
    # 16-bit and float sources load into float buffers (depth reports bits
    # of the buffer, not the file), so use is_float to keep 16-bit output.
    deep = img.is_float or img.depth in (48, 64)
    settings = {}
    if ext == '.png':
        fmt = 'PNG'
        settings['color_mode'] = 'RGBA' if has_alpha else 'RGB'
        settings['color_depth'] = '16' if deep else '8'
        settings['compression'] = 15
    elif ext in ('.jpg', '.jpeg'):
        fmt = 'JPEG'
        settings['color_mode'] = 'RGB'
        settings['quality'] = 95
    elif ext == '.tga':
        fmt = 'TARGA'
        settings['color_mode'] = 'RGBA' if has_alpha else 'RGB'
    elif ext == '.bmp':
        fmt = 'BMP'
        settings['color_mode'] = 'RGB'
    elif ext in ('.tif', '.tiff'):
        fmt = 'TIFF'
        settings['color_mode'] = 'RGBA' if has_alpha else 'RGB'
        settings['color_depth'] = '16' if deep else '8'
    elif ext == '.webp':
        fmt = 'WEBP'
        settings['color_mode'] = 'RGBA' if has_alpha else 'RGB'
        settings['quality'] = 95
    elif ext == '.exr':
        fmt = 'OPEN_EXR'
        settings['color_mode'] = 'RGBA' if has_alpha else 'RGB'
        settings['color_depth'] = '32' if img.depth in (96, 128) else '16'
        settings['exr_codec'] = 'ZIP'
    elif ext == '.hdr':
        fmt = 'HDR'
        settings['color_mode'] = 'RGB'
    else:
        fmt = 'PNG'
        settings['color_mode'] = 'RGBA'
    return fmt, settings


def process(scene, n_img, path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        w, h = img.size
        if w == 0 or h == 0:
            raise RuntimeError('image is empty, corrupt, or unsupported')

        n_img.image = img
        scene.render.resolution_x = w
        scene.render.resolution_y = h

        ext = os.path.splitext(path)[1].lower()
        fmt, settings = output_settings(img, ext)
        ims = scene.render.image_settings
        ims.file_format = fmt
        for key, value in settings.items():
            try:
                setattr(ims, key, value)
            except Exception:
                pass

        out_dir = os.path.join(os.path.dirname(path), OUT_DIR_NAME)
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, os.path.basename(path))
        scene.render.filepath = out_path
        bpy.ops.render.render(write_still=True)
        return out_path
    finally:
        n_img.image = None
        bpy.data.images.remove(img)


def main():
    argv = sys.argv
    if '--' not in argv or len(argv) <= argv.index('--') + 2:
        emit(type='fatal', msg='usage: ... -- <folder> <0|1 recursive>')
        return
    args = argv[argv.index('--') + 1:]
    root = os.path.abspath(args[0])
    recursive = args[1] == '1'

    if not os.path.isdir(root):
        emit(type='fatal', msg='folder does not exist: ' + root)
        return

    images = find_images(root, recursive)
    emit(type='total', count=len(images))
    if not images:
        emit(type='done', ok=0, err=0)
        return

    scene, n_img = setup_scene()
    ok = err = 0
    for i, path in enumerate(images, 1):
        try:
            out = process(scene, n_img, path)
            ok += 1
            emit(type='ok', i=i, path=path, out=out)
        except Exception as exc:
            err += 1
            emit(type='err', i=i, path=path,
                 msg=str(exc) or type(exc).__name__)
    emit(type='done', ok=ok, err=err)


try:
    main()
except Exception:
    emit(type='fatal', msg=traceback.format_exc(limit=5))
    sys.exit(1)
