"""Find a phrase and its time ranges in SubRip subtitles."""

import argparse
import csv
import html
import re
import sys
import unicodedata
from collections import deque
from pathlib import Path

TIMING = re.compile(r"^\s*(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{3})(?:\s|$)")


def milliseconds(stamp):
    hours, minutes, seconds, remainder = map(int, re.split(r"[:,.]", stamp))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + remainder


def words(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return re.findall(r"[^\W_]+", text)


def find_phrases(source, phrase):
    match = words(phrase)
    if not match:
        raise ValueError("Введи слово или фразу.")
    content = source.read_text(encoding="utf-8-sig")
    history = deque(maxlen=len(match))
    result = []
    last_end = 0
    for block in re.split(r"\n[ \t]*\n+", content.strip()):
        lines = block.splitlines()
        for index, line in enumerate(lines):
            timing = TIMING.match(line)
            if timing:
                break
        else:
            continue
        start, end = timing.groups()
        if milliseconds(start) - last_end > 1000 or milliseconds(end) < last_end:
            history.clear()
        last_end = milliseconds(end)
        text = " ".join(lines[index + 1:])
        plain = html.unescape(re.sub(r"<[^>]*>|\{\\[^}]*\}", "", text))
        for word in words(plain):
            history.append((word, start, end, plain))
            if len(history) == len(match) and [item[0] for item in history] == match:
                hit = (history[0][1], end, " / ".join(dict.fromkeys(item[3] for item in history)))
                if not result or result[-1] != hit:
                    result.append(hit)
    return result


def write_matches(source, phrase, destination):
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or destination.suffix.lower() != ".csv":
        raise ValueError("Укажи отдельный файл .csv.")
    matches = find_phrases(source, phrase)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(("Начало субтитра", "Конец субтитра", "Текст"))
        writer.writerows((start, end, "'" + text if text.lstrip().startswith(("=", "+", "-", "@"))
                         else text) for start, end, text in matches)
    return matches


def gui():
    from tkinter import Tk, filedialog, messagebox, simpledialog

    root = Tk()
    root.withdraw()
    try:
        selected = filedialog.askopenfilename(
            parent=root, title="Выбери субтитры", filetypes=[("SubRip", "*.srt")],
        )
        if not selected:
            return
        phrase = simpledialog.askstring("Поиск фразы", "Что найти в субтитрах?", parent=root)
        if not phrase:
            return
        source = Path(selected)
        output = filedialog.asksaveasfilename(
            parent=root, title="Сохрани найденные места", defaultextension=".csv",
            initialdir=source.parent, initialfile=f"{source.stem}-matches.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if output:
            matches = write_matches(source, phrase, Path(output))
            preview = "\n".join(f"{start} — {end}" for start, end, _ in matches[:10])
            messagebox.showinfo("Поиск фразы", f"Совпадений: {len(matches)}.\n{preview}\n\n{output}",
                                parent=root)
    except (OSError, ValueError) as exc:
        messagebox.showerror("Поиск фразы", str(exc), parent=root)
    finally:
        root.destroy()


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subtitles", type=Path)
    parser.add_argument("phrase")
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    output = args.output or args.subtitles.with_name(f"{args.subtitles.stem}-matches.csv")
    try:
        print(f"Совпадений: {len(write_matches(args.subtitles, args.phrase, output))}.")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
