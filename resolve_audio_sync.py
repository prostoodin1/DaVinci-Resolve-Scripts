"""Align a separately recorded microphone to the camera audio."""

import argparse
import os
import queue
import subprocess
import tempfile
import threading
from pathlib import Path

import numpy as np

from resolve_autocut import probe, run, tool

RATE = 2000
ANALYSIS_SECONDS = 180
MAX_OFFSET = 60


def read_reference(path):
    result = subprocess.run(
        [tool("ffmpeg"), "-v", "error", "-nostdin", "-i", str(path), "-map", "0:a:0",
         "-vn", "-t", str(ANALYSIS_SECONDS), "-af", "highpass=f=140,lowpass=f=850",
         "-ac", "1", "-ar", str(RATE), "-f", "f32le", "-"],
        capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode:
        raise ValueError("Нужна звуковая дорожка в обоих файлах.\n"
                         + result.stderr.decode("utf-8", errors="replace")[-700:])
    return np.frombuffer(result.stdout, dtype="<f4").astype(np.float64)


def find_offset(camera, microphone):
    """Return seconds by which to shift the microphone later on the camera clock."""
    window = 5 * RATE
    if min(len(camera), len(microphone)) < window:
        raise ValueError("Нужно минимум пять секунд звука в обоих файлах.")
    size = 1 << (len(camera) + window - 2).bit_length()
    spectrum = np.fft.rfft(camera, size)
    cumulative = np.concatenate(([0.0], np.cumsum(camera * camera)))
    offsets = []

    for start in range(0, len(microphone) - window + 1, window):
        template = microphone[start:start + window]
        power = np.dot(template, template)
        if power < window * 1e-6:
            continue
        left = max(0, start - MAX_OFFSET * RATE)
        right = min(len(camera) - window, start + MAX_OFFSET * RATE) + 1
        if right <= left:
            continue
        correlation = np.fft.irfft(spectrum * np.fft.rfft(template[::-1], size), size)
        energy = cumulative[left + window:right + window] - cumulative[left:right]
        scores = np.abs(correlation[left + window - 1:right + window - 1])
        scores /= np.sqrt(np.maximum(power * energy, 1e-10))
        scores[energy < window * 1e-6] = 0
        best = int(np.argmax(scores))
        score = float(scores[best])
        scores[max(0, best - RATE // 10):best + RATE // 10 + 1] = 0
        rival = float(np.max(scores))
        if score >= 0.22 and score > 1.18 * rival:
            offsets.append((left + best - start, score))

    if not offsets:
        raise ValueError("Не нашёл надёжного совпадения. Проверь, что оба микрофона записали один звук.")
    peak, score = max(offsets, key=lambda candidate: candidate[1])
    matching = [offset for offset, _ in offsets if abs(offset - peak) <= RATE * 0.025]
    if len(matching) * 2 <= len(offsets) or (len(matching) == 1 and score < 0.45):
        raise ValueError("Совпадение неоднозначно или дорожки расходятся со временем.")
    return float(np.median(matching)) / RATE


def synchronize(video, microphone, output):
    video = video.resolve(strict=True)
    microphone = microphone.resolve(strict=True)
    output = output.resolve()
    if output in (video, microphone) or output.suffix.lower() != ".wav":
        raise ValueError("Укажи отдельный выходной файл .wav.")
    info = probe(video)
    picture = next((s for s in info["streams"] if s["codec_type"] == "video"
                    and not s.get("disposition", {}).get("attached_pic")), None)
    camera_audio = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if picture is None or camera_audio is None:
        raise ValueError("Выбери видео, в котором сохранился звук камеры.")
    duration = float(picture.get("duration", info["format"].get("duration", 0)))
    if duration <= 0:
        raise ValueError("Не удалось прочитать длительность видео.")
    camera_start = float(camera_audio.get("start_time", info["format"].get("start_time", 0)))
    video_start = float(picture.get("start_time", info["format"].get("start_time", 0)))
    offset = find_offset(read_reference(video), read_reference(microphone)) + camera_start - video_start
    filters = ["asetpts=PTS-STARTPTS"]
    if offset > 0:
        filters.append(f"adelay={round(offset * 1000)}:all=1")
    elif offset < 0:
        filters.extend((f"atrim=start={-offset:.6f}", "asetpts=PTS-STARTPTS"))
    filters.extend(("apad", f"atrim=duration={duration:.9f}"))
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        candidate = Path(temporary) / "aligned.wav"
        run("ffmpeg", "-v", "error", "-nostdin", "-i", microphone, "-map", "0:a:0",
            "-af", ",".join(filters), "-c:a", "pcm_s24le", candidate)
        os.replace(candidate, output)
    return offset


def gui():
    from tkinter import Tk, filedialog, messagebox, ttk

    root = Tk()
    root.withdraw()
    try:
        video = filedialog.askopenfilename(parent=root, title="Выбери видео со звуком камеры")
        if not video:
            return
        microphone = filedialog.askopenfilename(parent=root, title="Выбери запись микрофона")
        if not microphone:
            return
        source = Path(video)
        output = filedialog.asksaveasfilename(
            parent=root, title="Сохрани синхронизированный звук", defaultextension=".wav",
            initialdir=source.parent, initialfile=f"{source.stem}-synced.wav",
            filetypes=[("WAV", "*.wav")],
        )
        if not output:
            return
        results = queue.SimpleQueue()

        def work():
            try:
                results.put((True, synchronize(source, Path(microphone), Path(output))))
            except (OSError, ValueError, KeyError) as exc:
                results.put((False, str(exc)))

        def poll():
            try:
                ok, result = results.get_nowait()
            except queue.Empty:
                root.after(150, poll)
                return
            if ok:
                messagebox.showinfo("Синхронизация", f"Сдвиг микрофона {result:+.3f} с.\n"
                                    f"Положи готовый WAV на начало видеоклипа.\n{output}", parent=root)
            else:
                messagebox.showerror("Синхронизация", result, parent=root)
            root.quit()

        root.title("Синхронизация микрофона")
        ttk.Label(root, text="Сравниваю звук камеры и микрофона…", padding=18).pack()
        root.deiconify()
        threading.Thread(target=work, daemon=True).start()
        root.after(150, poll)
        root.mainloop()
    finally:
        root.destroy()


def main():
    import sys
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", type=Path)
    parser.add_argument("microphone", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.video.with_name(f"{args.video.stem}-synced.wav")
    try:
        print(f"Сдвиг микрофона {synchronize(args.video, args.microphone, output):+.3f} с.")
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
