"""Transcribe video or audio to SRT with PocketSphinx's classical recognizer."""

import argparse
import os
import re
import sys
import tempfile
import textwrap
from pathlib import Path

from pocketsphinx import Decoder, Segmenter, get_model_path

from resolve_autocut import probe, run

RATE = 16000


def make_decoder(language):
    if language == "en":
        model = Path(get_model_path()) / "en-us"
        return Decoder(hmm=str(model / "en-us"), lm=str(model / "en-us.lm.bin"),
                       dict=str(model / "cmudict-en-us.dict"), loglevel="ERROR")
    model = Path(os.getenv("DRS_SPEECH_RU", Path(__file__).with_name("speech-ru")))
    if not model.is_dir():
        raise ValueError("Русский речевой пакет отсутствует. Запусти полный установщик.")
    language_model = model / "ru.lm.bin"
    if not language_model.is_file():
        language_model = model / "ru.lm"
    return Decoder(hmm=str(model), lm=str(language_model), dict=str(model / "ru.dic"),
                   loglevel="ERROR")


def recognize(raw, language):
    decoder = make_decoder(language)
    for segment in Segmenter(sample_rate=RATE).segment(raw):
        byte_limit = RATE * 2 * 18
        for offset in range(0, len(segment.pcm), byte_limit):
            chunk = segment.pcm[offset:offset + byte_limit]
            if len(chunk) < RATE // 3:
                continue
            decoder.start_utt()
            decoder.process_raw(chunk, False, True)
            decoder.end_utt()
            origin = segment.start_time + offset / (RATE * 2)
            if decoder.hyp() is None:
                continue
            for word in decoder.seg():
                spelling = re.sub(r"\(\d+\)$", "", word.word)
                if spelling and spelling[0] not in "<[+":
                    start = origin + word.start_frame / 100
                    end = min(origin + len(chunk) / (RATE * 2), origin + (word.end_frame + 1) / 100)
                    if end > start:
                        yield start, end, spelling


def stamp(seconds):
    minutes, milliseconds = divmod(max(0, round(seconds * 1000)), 60000)
    hours, minutes = divmod(minutes, 60)
    seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


def captions(words):
    first = last = 0
    line = []
    for start, end, word in words:
        if line and (start - last > 0.65 or end - first > 5
                     or len(" ".join([*line, word])) > 72):
            yield first, last, "\n".join(textwrap.wrap(" ".join(line), 40))
            line.clear()
        if not line:
            first = start
        line.append(word)
        last = end
    if line:
        yield first, last, "\n".join(textwrap.wrap(" ".join(line), 40))


def transcribe(source, output, language):
    source = source.resolve(strict=True)
    output = output.resolve()
    if output == source or output.suffix.lower() != ".srt":
        raise ValueError("Укажи отдельный выходной файл .srt.")
    info = probe(source)
    audio = next((stream for stream in info["streams"] if stream["codec_type"] == "audio"), None)
    video = next((stream for stream in info["streams"] if stream["codec_type"] == "video"
                  and not stream.get("disposition", {}).get("attached_pic")), None)
    if audio is None:
        raise ValueError("Выбери аудиофайл или видео со звуком.")
    start = float((video or audio).get("start_time", info["format"].get("start_time", 0)))
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        folder = Path(temporary)
        pcm = folder / "speech.pcm"
        run("ffmpeg", "-v", "error", "-nostdin", "-copyts", "-i", source,
            "-map", f"0:{audio['index']}", "-af",
            f"asetpts=PTS-({start:.9f})/TB,aresample={RATE}:first_pts=0",
            "-vn", "-ac", "1", "-ar", RATE, "-f", "s16le", pcm)
        srt = folder / "subtitles.srt"
        count = 0
        with pcm.open("rb") as audio_data, srt.open("w", encoding="utf-8", newline="\n") as handle:
            for count, (begin, end, text) in enumerate(captions(recognize(audio_data, language)), 1):
                handle.write(f"{count}\n{stamp(begin)} --> {stamp(end)}\n{text}\n\n")
        if not count:
            raise ValueError("Не удалось распознать речь. Проверь выбранный язык и слышимость речи.")
        os.replace(srt, output)
    return count


def gui():
    from resolve_project_ui import gui as project_gui
    project_gui("subtitles")


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("media", type=Path)
    parser.add_argument("--language", choices=("ru", "en"), default="ru")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.media.with_suffix(".srt")
    try:
        print(f"Сохранено строк: {transcribe(args.media, output, args.language)}.")
    except (OSError, ValueError, RuntimeError, KeyError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
