"""Create a Resolve timeline with cuts at abrupt changes in the picture."""

import argparse
import math
import queue
import re
import sys
import threading
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
    from tkinter import Tk, filedialog, messagebox, simpledialog, ttk

    root = Tk()
    root.withdraw()
    try:
        source = filedialog.askopenfilename(parent=root, title="Выбери видео для поиска склеек")
        if not source:
            return
        threshold = simpledialog.askfloat(
            "Поиск склеек", "Порог смены кадра. Меньше — больше склеек.",
            initialvalue=10, minvalue=0.01, maxvalue=100, parent=root,
        )
        if threshold is None:
            return
        source = Path(source)
        output = filedialog.asksaveasfilename(
            parent=root, title="Сохрани таймлайн", defaultextension=".fcpxml",
            initialdir=source.parent, initialfile=f"{source.stem}-scenes.fcpxml",
            filetypes=[("FCPXML", "*.fcpxml")],
        )
        if not output:
            return

        responses = queue.SimpleQueue()

        def analyze():
            try:
                responses.put((True, create_timeline(source, Path(output), threshold, 0.5)))
            except (ValueError, OSError, KeyError) as exc:
                responses.put((False, str(exc)))

        def poll():
            try:
                ok, message = responses.get_nowait()
            except queue.Empty:
                root.after(150, poll)
                return
            if ok:
                messagebox.showinfo("Поиск склеек", message + "\nИмпортируй .fcpxml в Resolve.", parent=root)
            else:
                messagebox.showerror("Поиск склеек", message, parent=root)
            root.quit()

        root.title("Поиск склеек")
        ttk.Label(root, text="Анализирую смены кадра…", padding=18).pack()
        root.deiconify()
        threading.Thread(target=analyze, daemon=True).start()
        root.after(150, poll)
        root.mainloop()
    except (ValueError, OSError, KeyError) as exc:
        messagebox.showerror("Поиск склеек", str(exc), parent=root)
    finally:
        root.destroy()


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
