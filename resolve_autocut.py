"""Create an editable DaVinci Resolve timeline from quiet sections of one video."""

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path


def tool(name):
    script_folder = Path(__file__).resolve().parent
    folders = [script_folder, script_folder / "FFmpeg" / "bin"]
    if os.environ.get("FFMPEG_DIR"):
        folders.insert(0, Path(os.environ["FFMPEG_DIR"]))
    for folder in folders:
        candidate = folder / (name + ".exe")
        if candidate.is_file():
            return str(candidate)
    result = shutil.which(name)
    if result:
        return result
    raise ValueError("Положи ffmpeg.exe и ffprobe.exe рядом со скриптом или добавь их в PATH.")


def run(name, *args):
    result = subprocess.run(
        [tool(name), *map(str, args)], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError(result.stderr.strip()[-1500:] or f"Ошибка {name}")
    return result


def source_timecode(info, video, fps):
    tags = [video.get("tags", {}), info["format"].get("tags", {})]
    tags.extend(stream.get("tags", {}) for stream in info["streams"])
    value = next((tag["timecode"] for tag in tags if tag.get("timecode")), None)
    if value is None:
        return 0
    match = re.fullmatch(r"(\d{2}):(\d{2}):(\d{2})([:;])(\d{2,3})", value)
    if not match:
        raise ValueError(f"Не удалось прочитать таймкод исходника {value}.")
    hours, minutes, seconds, separator, frames = match.groups()
    nominal_fps = round(fps)
    total_minutes = int(hours) * 60 + int(minutes)
    frame = (total_minutes * 60 + int(seconds)) * nominal_fps + int(frames)
    if separator == ";":
        if fps not in (Fraction(30000, 1001), Fraction(60000, 1001)):
            raise ValueError(f"Неподдерживаемый drop-frame таймкод {value}.")
        frame -= (nominal_fps // 15) * (total_minutes - total_minutes // 10)
    return frame


def keep_ranges(log, fps, frame_count, padding):
    seconds = r"([-+]?[\d.]+(?:e[-+]?\d+)?)"
    matches = re.finditer(r"silence_(start|end): " + seconds, log, re.IGNORECASE)
    start = None
    cursor = 0
    clips = []
    for match in matches:
        event, value = match.groups()
        value = Fraction(value)
        if event == "start":
            start = value
            continue
        if start is None:
            continue
        left = 0 if start <= 0 else math.ceil((start + padding) * fps)
        right = (
            frame_count if round(value * fps) >= frame_count
            else math.floor((value - padding) * fps)
        )
        start = None
        left = min(frame_count, max(cursor, left))
        right = min(frame_count, right)
        if right <= left:
            continue
        if left > cursor:
            clips.append((cursor, left))
        cursor = right
    if cursor < frame_count:
        clips.append((cursor, frame_count))
    return clips


def probe(source):
    return json.loads(run(
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", source
    ).stdout)


def create_timeline(source, destination, threshold, minimum, padding, external_audio=None):
    if not all(map(math.isfinite, (threshold, minimum, padding))):
        raise ValueError("Введи конечные числа.")
    if threshold >= 0 or minimum <= 0 or padding < 0:
        raise ValueError("Порог должен быть ниже 0 дБ, пауза больше 0, запас от 0.")
    source = source.resolve(strict=True)
    if external_audio is not None:
        external_audio = external_audio.resolve(strict=True)
        if external_audio == source:
            external_audio = None
    destination = destination.resolve()
    if source == destination or destination.suffix.lower() != ".fcpxml":
        raise ValueError("Укажи отдельный выходной файл с расширением .fcpxml.")

    info = probe(source)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio_info = probe(external_audio) if external_audio else info
    audio_streams = [s for s in audio_info["streams"] if s["codec_type"] == "audio"]
    if video is None or not audio_streams:
        raise ValueError("Выбери видео и отдельный аудиофайл либо видео со встроенным звуком.")
    audio = audio_streams[0]
    fps = Fraction(video.get("avg_frame_rate", "0/1"))
    duration = Fraction(video.get("duration", info["format"].get("duration", "0")))
    if fps <= 0 or duration <= 0:
        raise ValueError("Не удалось определить FPS или длительность видео.")
    frame_count = round(duration * fps)
    if frame_count < 1:
        raise ValueError("Видео короче одного кадра.")
    video_start = float(video.get("start_time", info["format"].get("start_time", 0)))
    timecode = source_timecode(info, video, fps)
    analysis_start = video_start
    if external_audio:
        analysis_start = float(audio.get("start_time", audio_info["format"].get("start_time", 0)))
        audio_duration = Fraction(audio.get("duration", audio_info["format"].get("duration", "0")))
        audio_frames = math.floor(audio_duration * fps)
        if audio_frames < 1:
            raise ValueError("Не удалось определить длительность отдельного звука.")

    log = run(
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-copyts", "-i",
        external_audio or source,
        "-map", f"0:{audio['index']}", "-vn",
        "-af", f"asetpts=PTS-({analysis_start:.9f})/TB,aresample=async=1:first_pts=0,"
               f"apad=whole_dur={float(duration):.9f},"
               f"atrim=duration={float(duration):.9f},silencedetect=n={threshold}dB:d={minimum}",
        "-f", "null", "-",
    ).stderr
    clips = keep_ranges(log, fps, frame_count, Fraction(str(padding)))
    if not clips:
        raise ValueError("Всё видео тише порога. Попробуй уменьшить порог, например до −45 дБ.")
    write_timeline(source, destination, video, fps, frame_count, timecode,
                   audio_streams, clips, "автонарезка", external_audio,
                   audio_frames if external_audio else None)
    total = sum(end - start for start, end in clips)
    removed = float((frame_count - total) / fps)
    return f"Клипов: {len(clips)}. Вырезано: {removed:.2f} сек."


def write_timeline(source, destination, video, fps, frame_count, timecode,
                   audio_streams, clips, label, external_audio=None, audio_frames=None):
    def time(frames):
        seconds = frames / fps
        if seconds.denominator == 1:
            return f"{seconds.numerator}s"
        return f"{seconds.numerator}/{seconds.denominator}s"

    total = sum(end - start for start, end in clips)
    name = f"{source.stem} — {label}"
    root = ET.Element("fcpxml", version="1.9")
    resources = ET.SubElement(root, "resources")
    ET.SubElement(resources, "format", id="r1", frameDuration=time(1),
                  width=str(video["width"]), height=str(video["height"]))
    asset = ET.SubElement(
        resources, "asset", id="r2", name=source.name, start=time(timecode),
        duration=time(frame_count), hasVideo="1", format="r1",
    )
    ET.SubElement(asset, "media-rep", kind="original-media", src=source.as_uri())
    audio_asset = asset
    if external_audio:
        audio_asset = ET.SubElement(resources, "asset", id="r3", name=external_audio.name,
                                    start="0s", duration=time(audio_frames))
        ET.SubElement(audio_asset, "media-rep", kind="original-media", src=external_audio.as_uri())
    if audio_streams:
        audio = audio_streams[0]
        audio_asset.attrib.update(hasAudio="1", audioSources=str(len(audio_streams)),
                                  audioChannels=str(audio["channels"]), audioRate=audio["sample_rate"])
    event = ET.SubElement(root, "event", name=label)
    project = ET.SubElement(event, "project", name=name)
    sequence = ET.SubElement(project, "sequence", format="r1", duration=time(total),
                             tcStart="0s", tcFormat="NDF")
    spine = ET.SubElement(sequence, "spine")
    offset = 0
    for start, end in clips:
        length = end - start
        clip = ET.SubElement(spine, "asset-clip", ref="r2", name=source.stem, offset=time(offset),
                             start=time(timecode + start), duration=time(length))
        if external_audio and start < audio_frames:
            ET.SubElement(clip, "audio", ref="r3", name=external_audio.stem, lane="-1",
                          offset=time(timecode + start), start=time(start),
                          duration=time(min(end, audio_frames) - start))
        offset += length
    ET.indent(root)
    destination.write_bytes(
        b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        + ET.tostring(root, encoding="utf-8") + b"\n"
    )


def gui():
    from resolve_project_ui import gui as project_gui
    project_gui("autocut")


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--audio", type=Path, help="Separate audio file aligned to the video start")
    parser.add_argument("--threshold", type=float, default=-35)
    parser.add_argument("--min-silence", type=float, default=0.65)
    parser.add_argument("--padding", type=float, default=0.12)
    args = parser.parse_args()
    output = args.output or args.video.with_name(f"{args.video.stem}-autocut.fcpxml")
    try:
        print(create_timeline(args.video, output, args.threshold, args.min_silence, args.padding,
                              external_audio=args.audio))
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
