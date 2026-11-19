"""Create a Resolve timeline with cuts at abrupt changes in the picture."""

import argparse
import math
import re
import sys
from fractions import Fraction
from pathlib import Path

from resolve_autocut import probe, run, source_timecode, write_timeline


def create_timeline(source, destination, threshold, minimum):
    if not 0 < threshold <= 100 or not math.isfinite(minimum) or minimum < 0:
        raise ValueError("Порог должен быть больше 0 и не выше 100, минимальная длительность от 0.")
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if destination == source or destination.suffix.lower() != ".fcpxml":
        raise ValueError("Укажи отдельный выходной файл с расширением .fcpxml.")

    info = probe(source)
    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("Нужен файл с видео.")
    audio = [stream for stream in info["streams"] if stream["codec_type"] == "audio"]
    fps = Fraction(video.get("avg_frame_rate", "0/1"))
    duration = Fraction(video.get("duration", info["format"].get("duration", "0")))
    frame_count = round(duration * fps)
    if fps <= 0 or frame_count < 1:
        raise ValueError("Не удалось определить FPS или длительность видео.")
    min_frames = max(1, math.ceil(Fraction(str(minimum)) * fps))

    log = run(
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", source,
        "-map", f"0:{video['index']}", "-an",
        "-vf", f"setpts=PTS-STARTPTS,scale=320:-2,scdet=threshold={threshold}",
        "-f", "null", "-",
    ).stderr
    boundaries = [0]
    for value in re.findall(r"lavfi\.scd\.time:\s*(\S+)", log):
        frame = round(Fraction(value) * fps)
        if frame - boundaries[-1] >= min_frames and frame_count - frame >= min_frames:
            boundaries.append(frame)
    boundaries.append(frame_count)
    clips = list(zip(boundaries, boundaries[1:]))
    write_timeline(source, destination, video, fps, frame_count,
                   source_timecode(info, video, fps), audio, clips, "смены кадра")
    return f"Склеек: {len(clips) - 1}. Клипов: {len(clips)}."


def gui():
    from resolve_project_ui import gui as project_gui
    project_gui("scenes")


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--threshold", type=float, default=10)
    parser.add_argument("--min-shot", type=float, default=0.5)
    args = parser.parse_args()
    output = args.output or args.video.with_name(f"{args.video.stem}-scenes.fcpxml")
    try:
        print(create_timeline(args.video, output, args.threshold, args.min_shot))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
