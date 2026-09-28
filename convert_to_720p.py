#!/usr/bin/env python3
r"""Convert every video in a folder to 720p, leaving the originals untouched.

Pick a folder (a dialog opens, or pass the path as an argument). Each video that is
larger than 720p is downscaled and saved as "<name>_720p" in a "720p" subfolder
inside that folder. Videos that are already 720p or smaller are skipped, and so are
videos converted on an earlier run, so it is safe to run the script again.

Scaling uses ffmpeg's "area" filter, which averages the light over each new, larger
pixel the way a 720p camera sensor does; in testing it came out closest to footage
shot natively at 720p. Use --scaler spline36 (or lanczos) for a slightly crisper look.

How to run it on Windows:
    1. Install Python (python.org) and ffmpeg (in PowerShell: winget install Gyan.FFmpeg).
       Instead of winget you can also put ffmpeg.exe and ffprobe.exe next to this script.
    2. Double-click this file and choose the folder, or drag a folder onto this file,
       or run it from a terminal:
           py convert_to_720p.py "D:\Videos\Holiday"
           py convert_to_720p.py "D:\Videos\Holiday" --scaler spline36
On macOS/Linux install ffmpeg with brew/apt and run: python3 convert_to_720p.py [folder]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

OUTPUT_FOLDER = "720p"  # created inside the chosen folder
SUFFIX = "_720p"  # clip.mov -> 720p/clip_720p.mov
CRF_H264 = 18  # quality for normal video: lower = better and bigger (18 ~ visually lossless)
CRF_HEVC = 20  # quality for HDR video (e.g. iPhone HDR), which stays 10-bit HDR (HEVC)
PRESET_H264 = "slow"  # slower presets give smaller files at the same quality
PRESET_HEVC = "medium"  # HEVC is much slower to encode, so a faster preset

VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".asf", ".flv", ".f4v", ".webm", ".mts",
    ".m2ts", ".ts", ".m2t", ".mpg", ".mpeg", ".vob", ".3gp", ".3g2", ".mxf", ".ogv", ".dv",
}
MUXERS = {".mp4": "mp4", ".m4v": "mp4", ".mov": "mov", ".mkv": "matroska"}  # others are saved as .mp4
INTERLACED = {"tt", "bb", "tb", "bt"}
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # HDR10 / HLG


def find_tool(name):
    """Return the path of ffmpeg/ffprobe: on the PATH, next to this script, or from winget."""
    found = shutil.which(name)
    if found:
        return found
    exe = name + (".exe" if os.name == "nt" else "")
    places = [Path(__file__).resolve().parent]
    if os.environ.get("LOCALAPPDATA"):
        # winget's install folder; lets a double-click work before Windows refreshes the PATH
        places.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft" / "WinGet" / "Links")
    for place in places:
        if (place / exe).is_file():
            return str(place / exe)
    return None


def choose_folder():
    """Ask for the folder with a dialog, or in the terminal if no dialog can be shown."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(title="Choose the folder with your videos")
        root.destroy()
        return chosen
    except Exception:
        return input("Folder with your videos: ").strip().strip('"')


def probe(ffprobe, path):
    cmd = [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def rotation(video):
    """Rotation the player applies (phones store portrait video as rotated landscape)."""
    for side_data in video.get("side_data_list", []):
        if "rotation" in side_data:
            return int(round(float(side_data["rotation"]))) % 360
    tag = video.get("tags", {}).get("rotate", "0")
    return int(tag) % 360 if tag.lstrip("-").isdigit() else 0


def display_size(video):
    """Width and height as shown on screen: after pixel aspect ratio and rotation."""
    width, height = float(video["width"]), float(video["height"])
    num, _, den = (video.get("sample_aspect_ratio") or "1:1").partition(":")
    if num.isdigit() and den.isdigit() and int(num) and int(den):
        width = width * int(num) / int(den)
    if rotation(video) in (90, 270):
        width, height = height, width
    return width, height


def target_size(width, height):
    """Size that fits in 1280x720 (or 720x1280 for portrait), or None if already that small."""
    box_w, box_h = (1280, 720) if width >= height else (720, 1280)
    if width <= box_w and height <= box_h:
        return None
    scale = min(box_w / width, box_h / height)
    return tuple(max(2, int(round(side * scale / 2)) * 2) for side in (width, height))


def can_copy_audio(codec, out_ext):
    if out_ext == ".mkv":
        return True
    if codec in {"aac", "mp3", "alac", "ac3", "eac3"}:
        return True
    return out_ext == ".mov" and codec.startswith("pcm_")


def main_video_stream(info):
    """First real video stream (not an embedded cover picture), or None."""
    return next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                 and s.get("width") and s.get("height")
                 and not s.get("disposition", {}).get("attached_pic")), None)


def build_command(ffmpeg, src, dst, info, video, size, scaler, encoders):
    # First audio track that ffmpeg can decode (skips e.g. Apple's spatial-audio track).
    audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"
                  and s.get("codec_name") not in (None, "", "none")), None)
    out_ext = dst.suffix.lower()

    filters = []
    if video.get("field_order") in INTERLACED:
        filters.append("bwdif=mode=send_field")  # 1080i50/60 -> 720p50/60, like broadcast 720p
    width, height = size
    if scaler == "spline36":
        filters.append(f"zscale=w={width}:h={height}:filter=spline36")
    else:
        filters.append(f"scale={width}:{height}:flags={scaler}")
    filters.append("setsar=1")

    cmd = [ffmpeg, "-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-i", str(src), "-map", f"0:{video['index']}", "-vf", ",".join(filters)]

    hdr = video.get("color_transfer") in HDR_TRANSFERS
    if hdr and "libx265" in encoders:
        # HDR stays 10-bit HDR (HEVC, like the phone recorded it) instead of turning washed out.
        cmd += ["-c:v", "libx265", "-preset", PRESET_HEVC, "-crf", str(CRF_HEVC),
                "-pix_fmt", "yuv420p10le", "-x265-params", "log-level=error"]
        if out_ext != ".mkv":
            cmd += ["-tag:v", "hvc1"]  # needed for Apple devices to play HEVC
        codec_label = "HEVC 10-bit HDR"
    else:
        # Everything else becomes 8-bit H.264, which plays everywhere (incl. Windows' own player).
        pix_fmt = "yuv420p10le" if hdr else "yuv420p"
        cmd += ["-c:v", "libx264", "-preset", PRESET_H264, "-crf", str(CRF_H264), "-pix_fmt", pix_fmt]
        codec_label = "H.264" + (" 10-bit HDR" if hdr else "")
    for key, option in (("color_primaries", "-color_primaries"), ("color_transfer", "-color_trc"),
                        ("color_space", "-colorspace")):
        value = video.get(key)
        if value and value not in ("unknown", "unspecified", "reserved"):
            cmd += [option, value]

    if audio is None:
        cmd += ["-an"]
    else:
        cmd += ["-map", f"0:{audio['index']}"]
        if can_copy_audio(audio["codec_name"], out_ext):
            cmd += ["-c:a", "copy"]
        else:
            channels = int(audio.get("channels") or 2)
            cmd += ["-c:a", "aac", "-b:a", "128k" if channels == 1 else "192k" if channels == 2 else "384k"]

    if out_ext == ".mkv":
        cmd += ["-map", "0:s?", "-c:s", "copy", "-map", "0:t?"]  # keep subtitles and their fonts
    else:
        cmd += ["-movflags", "+faststart+use_metadata_tags"]  # keep date/location metadata
    cmd += ["-map_metadata", "0", "-max_muxing_queue_size", "4096", "-f", MUXERS[out_ext], str(dst)]
    return cmd, codec_label


def clock(seconds):
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def run_ffmpeg(cmd, duration):
    """Run ffmpeg, showing its progress. Returns (exit code, error text)."""
    show = sys.stdout.isatty()
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors,
                                text=True, encoding="utf-8", errors="replace")
        try:
            done, speed = 0.0, ""
            for line in proc.stdout:
                key, _, value = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):
                    try:
                        done = int(value) / 1_000_000
                    except ValueError:
                        pass
                elif key == "speed":
                    speed = value
                elif key == "progress" and show:
                    percent = f"{min(done / duration * 100, 100):5.1f}%  " if duration else ""
                    total = f" / {clock(duration)}" if duration else ""
                    print(f"\r      {percent}{clock(done)}{total}  {speed:>7}", end="", flush=True)
            proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait()
            raise
        finally:
            if show:
                print("\r" + " " * 60 + "\r", end="", flush=True)
        errors.seek(0)
        return proc.returncode, errors.read().decode("utf-8", "replace").strip()


def move_into_place(tmp, dst):
    """Rename the finished file; retry briefly in case antivirus is still scanning it."""
    for attempt in range(20):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.5)


def plan_output_names(videos):
    """Map each video to its output file name, keeping names unique (clip.mp4 + clip.avi)."""
    names, used = {}, set()
    # Videos whose container is kept get first pick of the plain "<name>_720p" name.
    for src in sorted(videos, key=lambda p: (p.suffix.lower() not in MUXERS, p.name.lower())):
        ext = src.suffix.lower() if src.suffix.lower() in MUXERS else ".mp4"
        name = f"{src.stem}{SUFFIX}{ext}"
        if name.lower() in used:
            name = f"{src.stem}_{src.suffix.lower().lstrip('.')}{SUFFIX}{ext}"
        used.add(name.lower())
        names[src] = name
    return names


def main():
    parser = argparse.ArgumentParser(description="Convert every video in a folder to 720p.")
    parser.add_argument("folder", nargs="?", help="folder with the videos (a dialog opens if omitted)")
    parser.add_argument("--scaler", default="area", choices=["area", "bicubic", "spline36", "lanczos"],
                        help="area (default) is closest to native 720p; spline36/lanczos are crisper")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")

    ffmpeg, ffprobe = find_tool("ffmpeg"), find_tool("ffprobe")
    if not ffmpeg or not ffprobe:
        how = "winget install Gyan.FFmpeg" if os.name == "nt" else "brew/apt install ffmpeg"
        print(f"ffmpeg was not found. Install it (for example:  {how}), then run this again.")
        return 1
    encoders = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    if "libx264" not in encoders:
        print("This ffmpeg build has no H.264 encoder (libx264). Install a full ffmpeg build.")
        return 1
    scaler = args.scaler
    if scaler == "spline36":
        filters = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        if " zscale " not in filters:
            print("This ffmpeg build has no zscale filter (needed for spline36); using lanczos instead.")
            scaler = "lanczos"

    folder = args.folder or choose_folder()
    if not folder:
        print("No folder chosen.")
        return 1
    folder = Path(folder).expanduser().resolve()
    if not folder.is_dir():
        print(f"Not a folder: {folder}")
        return 1

    videos = [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS
              and not p.name.startswith(".")]  # skips macOS "._clip.mov" helper files
    videos.sort(key=lambda p: p.name.lower())
    if not videos:
        print(f"No video files found in {folder}")
        return 0
    out_dir = folder / OUTPUT_FOLDER
    names = plan_output_names(videos)
    print(f"Found {len(videos)} video(s) in {folder}\nSaving 720p copies to {out_dir}\n")

    converted, small, existing, unreadable, failed = [], [], [], [], []
    started = time.time()
    for number, src in enumerate(videos, 1):
        dst = out_dir / names[src]
        prefix = f"[{number}/{len(videos)}] {src.name}"
        if dst.exists():
            print(f"{prefix}: already converted, skipped")
            existing.append(src)
            continue
        info = probe(ffprobe, src)
        video = main_video_stream(info) if info else None
        if video is None:
            print(f"{prefix}: could not be read as a video, skipped")
            unreadable.append(src)
            continue
        width, height = display_size(video)
        size = target_size(width, height)
        if size is None:
            print(f"{prefix}: already {round(width)}x{round(height)}, skipped")
            small.append(src)
            continue

        out_dir.mkdir(exist_ok=True)
        tmp = dst.with_name(dst.stem + ".partial" + dst.suffix)
        cmd, codec_label = build_command(ffmpeg, src, tmp, info, video, size, scaler, encoders)
        interlaced = ", deinterlaced" if video.get("field_order") in INTERLACED else ""
        print(f"{prefix}: {round(width)}x{round(height)} -> {size[0]}x{size[1]}, {codec_label}{interlaced}")
        try:
            duration = float(info.get("format", {}).get("duration") or 0)
        except ValueError:
            duration = 0
        file_started = time.time()
        try:
            code, error_text = run_ffmpeg(cmd, duration)
            if code == 0 and tmp.is_file():
                move_into_place(tmp, dst)
                stat = src.stat()
                os.utime(dst, (stat.st_atime, stat.st_mtime))  # keep the original date
                print(f"      done in {clock(time.time() - file_started)} -> {dst.name}")
                converted.append(src)
            else:
                reason = " | ".join(error_text.splitlines()[-3:]) or f"ffmpeg stopped (exit code {code})"
                print("      FAILED: " + reason)
                failed.append(src)
        except KeyboardInterrupt:
            print("\nStopped. The unfinished file was removed; run again to continue.")
            return 130
        finally:
            try:
                tmp.unlink()  # only still there if the conversion did not finish
            except OSError:
                pass

    print(f"\nFinished in {clock(time.time() - started)}: {len(converted)} converted, "
          f"{len(small)} already 720p or smaller, {len(existing)} converted earlier, "
          f"{len(unreadable)} unreadable, {len(failed)} failed.")
    for src in unreadable:
        print(f"  unreadable: {src.name}")
    for src in failed:
        print(f"  failed: {src.name}")
    return 1 if failed or unreadable else 0


if __name__ == "__main__":
    try:
        exit_code = main()
    except KeyboardInterrupt:
        exit_code = 130
    except Exception:
        import traceback

        traceback.print_exc()
        exit_code = 1
    if os.name == "nt" and sys.stdin is not None and sys.stdin.isatty():
        input("\nPress Enter to close...")  # keeps the window open when double-clicked
    sys.exit(exit_code)
