"""Group similar lines in SRT subtitles into possible repeated takes."""

import argparse
import csv
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from resolve_phrase_search import csv_text, milliseconds, read_cues, words


@dataclass(frozen=True)
class Passage:
    start: str
    end: str
    text: str
    tokens: tuple[str, ...]
    start_ms: int
    end_ms: int


def passages(source, minimum):
    cues = sorted((milliseconds(start), milliseconds(end), start, end, text)
                  for start, end, text in read_cues(source) if text.strip())
    for index, (start_ms, end_ms, start, end, text) in enumerate(cues):
        if end_ms <= start_ms:
            continue
        tokens = tuple(words(text))
        if len(tokens) >= minimum:
            yield Passage(start, end, text, tokens, start_ms, end_ms)
        if index + 1 < len(cues):
            next_start, next_end, _, end_stamp, next_text = cues[index + 1]
            if 0 <= next_start - end_ms <= 850 and next_end > next_start and next_end - start_ms <= 9000:
                joined = tokens + tuple(words(next_text))
                if minimum <= len(joined) <= 36:
                    yield Passage(start, end_stamp, text + " " + next_text, joined, start_ms, next_end)


def overlaps(first, second):
    return first.start_ms < second.end_ms and second.start_ms < first.end_ms


def find_takes(source, threshold=0.80, minimum=5):
    if not 0.60 <= threshold <= 1 or not 4 <= minimum <= 30:
        raise ValueError("Похожесть должна быть от 60 до 100%, длина фразы — от 4 до 30 слов.")
    ordered = sorted(passages(source, minimum), key=lambda part: (-len(part.tokens), part.start_ms))
    word_index = defaultdict(list)
    for index, part in enumerate(ordered):
        for word in set(part.tokens):
            word_index[word].append(index)

    used = []
    groups = []
    for index, first in enumerate(ordered):
        if any(overlaps(first, part) for part in used):
            continue
        candidates = Counter(other for word in set(first.tokens) for other in word_index[word]
                             if other > index)
        matches = []
        for other, shared in candidates.items():
            second = ordered[other]
            if shared < 2 and first.tokens != second.tokens:
                continue
            shorter, longer = sorted((len(first.tokens), len(second.tokens)))
            if 2 * shorter / (shorter + longer) < threshold - 0.1 or overlaps(first, second):
                continue
            word_score = SequenceMatcher(None, first.tokens, second.tokens, autojunk=False).ratio()
            if word_score < 0.55:
                continue
            character_score = SequenceMatcher(None, " ".join(first.tokens), " ".join(second.tokens),
                                              autojunk=False).ratio()
            score = 0.65 * word_score + 0.35 * character_score
            if score >= threshold:
                matches.append((second, score))
        group = [(first, 1.0)]
        for part, score in sorted(matches, key=lambda item: (-item[1], item[0].start_ms)):
            if not any(overlaps(part, chosen) for chosen, _ in group) and not any(
                    overlaps(part, chosen) for chosen in used):
                group.append((part, score))
        if len(group) >= 2:
            used.extend(part for part, _ in group)
            groups.append(sorted(group, key=lambda item: item[0].start_ms))
    return sorted(groups, key=lambda group: group[0][0].start_ms)


def write_takes(source, destination, threshold=0.80, minimum=5):
    source, destination = source.resolve(strict=True), destination.resolve()
    if source == destination or destination.suffix.lower() != ".csv":
        raise ValueError("Укажи отдельный файл .csv.")
    groups = find_takes(source, threshold, minimum)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle, delimiter=";")
        writer.writerow(("Группа", "Начало", "Конец", "Похожесть, %", "Фраза"))
        for number, group in enumerate(groups, 1):
            for part, score in group:
                writer.writerow((number, part.start, part.end, round(score * 100), csv_text(part.text)))
    return groups


def gui():
    from tkinter import Tk, filedialog, messagebox, simpledialog

    root = Tk()
    root.withdraw()
    try:
        path = filedialog.askopenfilename(parent=root, title="Выбери SRT с исходной записью",
                                          filetypes=[("SubRip", "*.srt")])
        if not path:
            return
        score = simpledialog.askinteger("Повторные дубли", "Минимальная похожесть в процентах",
                                        initialvalue=80, minvalue=60, maxvalue=100, parent=root)
        if score is None:
            return
        source = Path(path)
        output = filedialog.asksaveasfilename(
            parent=root, title="Сохрани найденные дубли", defaultextension=".csv",
            initialdir=source.parent, initialfile=f"{source.stem}-takes.csv",
            filetypes=[("CSV", "*.csv")],
        )
        if output:
            groups = write_takes(source, Path(output), score / 100)
            messagebox.showinfo("Повторные дубли",
                                f"Групп: {len(groups)}. Попыток: {sum(map(len, groups))}.\n{output}",
                                parent=root)
    except (OSError, ValueError) as exc:
        messagebox.showerror("Повторные дубли", str(exc), parent=root)
    finally:
        root.destroy()


def main():
    if len(sys.argv) == 1:
        gui()
        return
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("subtitles", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    parser.add_argument("--threshold", type=int, default=80, help="Similarity percentage")
    parser.add_argument("--min-words", type=int, default=5)
    args = parser.parse_args()
    output = args.output or args.subtitles.with_name(f"{args.subtitles.stem}-takes.csv")
    try:
        print(f"Групп: {len(write_takes(args.subtitles, output, args.threshold / 100, args.min_words))}.")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
