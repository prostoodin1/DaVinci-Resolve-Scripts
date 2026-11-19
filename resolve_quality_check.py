"""Find black frames and frozen pictures in a finished video."""

import argparse
import csv
import re
import sys
from pathlib import Path

from resolve_autocut import probe, run


def inspect(source, output):
    source = source.resolve(strict=True)
    output = output.resolve()
    if output == source or output.suffix.lower() != ".csv":
        raise ValueError("Укажи отдельный файл .csv для отчёта.")
    info = probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        raise ValueError("Нужен файл с видео.")
    duration = float(video.get("duration", info["format"].get("duration", 0)))
    log = run(
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", source,
        "-map", f"0:{video['index']}", "-an",
        "-vf", "setpts=PTS-STARTPTS,scale=320:-2,tpad=stop=1:stop_mode=clone,"
               "blackdetect=d=0.25,freezedetect=n=-50dB:d=2",
        "-f", "null", "-",
    ).stderr
    spans = []
    for start, end in re.findall(r"black_start:(\S+)\s+black_end:(\S+)", log):
        spans.append((float(start), float(end), "Чёрный экран"))
    freeze_start = None
    for kind, value in re.findall(r"lavfi\.freezedetect\.freeze_(start|end):\s*(\S+)", log):
        if kind == "start":
            freeze_start = float(value)
        elif freeze_start is not None:
            spans.append((freeze_start, float(value), "Статичная картинка"))
            freeze_start = None
    if freeze_start is not None and duration > freeze_start:
        spans.append((freeze_start, duration, "Статичная картинка"))

    def stamp(seconds):
        whole, milliseconds = divmod(round(seconds * 1000), 1000)
        hours, remainder = divmod(whole, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02}:{minutes:02}:{seconds:02}.{milliseconds:03}"

    with output.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(("Событие", "Начало от начала видео", "Конец от начала видео"))
        writer.writerows((kind, stamp(start), stamp(min(end, duration)))
                         for start, end, kind in sorted(spans) if start < min(end, duration))
    return len(spans)


def gui():
    from resolve_project_ui import gui as project_gui
    project_gui("quality")


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.video.with_name(f"{args.video.stem}-check.csv")
    try:
        print(f"Участков в отчёте: {inspect(args.video, output)}.")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
