"""Project media browser shared by Resolve editing tools."""

import importlib.util
import os
import queue
import re
import sys
import threading
import tempfile
from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import ttk


@dataclass
class ProjectClip:
    name: str
    path: Path
    folder: str
    kind: str
    duration: str
    selected: bool
    has_audio: bool
    has_video: bool


def media_flags(properties):
    """Use stream metadata, with the Media Pool type as a fallback."""
    kind = str(properties.get("Type", "")).casefold()
    channels = re.match(r"\s*(\d+)", str(properties.get("Audio Ch", "")))
    audio_codec = str(properties.get("Audio Codec", "") or "").strip().casefold()
    video_codec = str(properties.get("Video Codec", "") or "").strip().casefold()
    audio = audio_codec not in ("", "none", "n/a", "unknown", "0")
    audio = audio or bool(channels and int(channels[1]) > 0) or "audio" in kind
    video = video_codec not in ("", "none", "n/a", "unknown", "0") or "video" in kind
    if "still" in kind:
        video = False
    return audio, video


def connect():
    modules = Path(os.environ.get("RESOLVE_SCRIPT_API",
                   str(Path(os.environ["PROGRAMDATA"]) / "Blackmagic Design" /
                       "DaVinci Resolve" / "Support" / "Developer" / "Scripting"))) / "Modules"
    location = modules / "DaVinciResolveScript.py"
    if not location.is_file():
        raise ValueError("Не найден модуль Resolve. Запусти установленный DaVinci Resolve.")
    spec = importlib.util.spec_from_file_location("DaVinciResolveScript", location)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api = sys.modules.get("DaVinciResolveScript", api)
    resolve = api.scriptapp("Resolve")
    if not resolve:
        raise ValueError("Нет подключения к Resolve. Открой проект и проверь доступ к локальным скриптам.")
    project = resolve.GetProjectManager().GetCurrentProject()
    if not project:
        raise ValueError("Открой проект в DaVinci Resolve.")
    return resolve, project


def project_clips(project):
    pool = project.GetMediaPool()
    chosen = {item.GetMediaId() for item in (pool.GetSelectedClips() or [])}
    clips = []
    pending = [(pool.GetRootFolder(), "")]
    while pending:
        folder, parent = pending.pop()
        name = parent + "/" + folder.GetName() if parent else folder.GetName()
        pending.extend((child, name) for child in reversed(folder.GetSubFolderList() or []))
        for item in folder.GetClipList() or []:
            properties = item.GetClipProperty()
            path = properties.get("File Path", "")
            if not path:
                continue
            kind = properties.get("Type", "")
            audio, video = media_flags(properties)
            clips.append(ProjectClip(
                item.GetName(), Path(path), name, kind, properties.get("Duration", ""),
                item.GetMediaId() in chosen, audio, video,
            ))
    return clips


def output_path(project_name, filename):
    safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", project_name).strip(". ")[:70] or "Project"
    folder = Path(os.environ["LOCALAPPDATA"]) / "DaVinci Resolve Scripts" / "Results" / safe_name
    folder.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(dir=folder)) / filename


TOOLS = {
    "audio-sync": ("Синхронизация микрофона", "Синхронизировать", "Видео со звуком камеры"),
    "autocut": ("Нарезка по тишине", "Собрать таймлайн", "Видео"),
    "scenes": ("Поиск склеек", "Найти склейки", "Видео"),
    "quality": ("Проверка экспорта", "Проверить", "Видео"),
    "subtitles": ("Речь в субтитры", "Распознать речь", "Видео или аудио"),
}


def process(kind, source, audio, destination, settings):
    if kind == "audio-sync":
        from resolve_audio_sync import synchronize
        offset = synchronize(source, audio, destination)
        return f"Сдвиг микрофона {offset:+.3f} с."
    if kind == "autocut":
        from resolve_autocut import create_timeline
        return create_timeline(source, destination, float(settings["threshold"]),
                               float(settings["minimum"]), float(settings["padding"]), audio)
    if kind == "scenes":
        from resolve_scene_cuts import create_timeline
        return create_timeline(source, destination, float(settings["threshold"]), 0.5)
    if kind == "quality":
        from resolve_quality_check import inspect
        return f"Участков для проверки: {inspect(source, destination)}."
    from resolve_subtitles import transcribe
    return f"Строк субтитров: {transcribe(source, destination, settings['language'])}."


class ProjectTool:
    def __init__(self, root, kind):
        self.root, self.kind = root, kind
        self.project = self.resolve = None
        self.clips = []
        self.busy = False
        title, action, primary = TOOLS[kind]
        root.title(title + " · DaVinci Resolve")
        root.geometry("1180x740")
        root.minsize(1000, 620)
        root.configure(bg="#242424")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background="#242424", foreground="#e8e8e8", font=("Segoe UI", 10))
        style.configure("TFrame", background="#242424")
        style.configure("TLabel", background="#242424")
        style.configure("Title.TLabel", font=("Segoe UI", 21, "bold"))
        style.configure("Muted.TLabel", foreground="#a6a6a6")
        style.configure("Selection.TLabel", foreground="#ef9b55", font=("Segoe UI", 10))
        style.configure("TButton", padding=(16, 9), background="#383838", borderwidth=0)
        style.map("TButton", background=[("active", "#484848"), ("disabled", "#2d2d2d")],
                  foreground=[("disabled", "#777777")])
        style.configure("Accent.TButton", background="#ec8b3c", foreground="#202020")
        style.map("Accent.TButton", background=[("active", "#ffa255"), ("disabled", "#383838")],
                  foreground=[("disabled", "#777777")])
        style.configure("Treeview", background="#2b2b2b", fieldbackground="#2b2b2b",
                        foreground="#dddddd", rowheight=44, borderwidth=0)
        style.map("Treeview", background=[("selected", "#3a3a3a")],
                  foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background="#323232", foreground="#aaaaaa",
                        padding=(8, 12), relief="flat")
        style.map("Treeview.Heading", background=[("active", "#3a3a3a")])
        style.configure("TEntry", fieldbackground="#303030", foreground="#eeeeee", padding=8,
                        bordercolor="#444444", insertcolor="#ec8b3c")
        style.configure("TCombobox", fieldbackground="#303030", foreground="#eeeeee", padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", "#303030")],
                  foreground=[("readonly", "#eeeeee")])
        style.configure("Horizontal.TProgressbar", background="#ec8b3c", troughcolor="#333333",
                        borderwidth=0, thickness=4)
        style.configure("TScrollbar", background="#4a4a4a", troughcolor="#2b2b2b",
                        borderwidth=0, arrowsize=12)
        self.dots = (self.dot(False), self.dot(True))
        self.selection_labels = {}
        self.filters = {}
        self.chosen = {}

        panel = ttk.Frame(root, padding=26)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=title, style="Title.TLabel").pack(anchor="w")
        self.heading = tk.StringVar(value="Подключаюсь к открытому проекту…")
        ttk.Label(panel, textvariable=self.heading, style="Muted.TLabel").pack(anchor="w", pady=(6, 18))
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x", pady=(0, 12))
        ttk.Label(toolbar, text="МЕДИА ПРОЕКТА", style="Muted.TLabel").pack(side="left")
        self.refresh = ttk.Button(toolbar, text="Обновить проект", command=self.load)
        self.refresh.pack(side="right")
        tables = ttk.Panedwindow(panel, orient="horizontal")
        tables.pack(fill="both", expand=True)
        self.primary = self.table(tables, primary)
        self.secondary = None
        if kind in ("audio-sync", "autocut"):
            caption = "Аудио для синхронизации" if kind == "audio-sync" else "Звук для анализа"
            self.secondary = self.table(tables, caption)
            if kind == "autocut":
                ttk.Label(panel, text="Звук из видео используется, пока справа не выбрана другая запись.",
                          style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
                self.clear_audio = ttk.Button(panel, text="Использовать звук видео",
                    command=self.use_camera_audio)
                self.clear_audio.pack(anchor="w", pady=(4, 0))
        self.settings = {}
        options = ttk.Frame(panel)
        options.pack(fill="x", pady=(18, 4))
        if kind in ("autocut", "scenes"):
            self.setting(options, "Порог, дБ" if kind == "autocut" else "Порог смены кадра",
                         "threshold", "-35" if kind == "autocut" else "10")
        if kind == "autocut":
            self.setting(options, "Пауза, с", "minimum", "0.65")
            self.setting(options, "Запас, с", "padding", "0.12")
        if kind == "subtitles":
            self.settings["language"] = tk.StringVar(value="ru")
            ttk.Label(options, text="Язык").pack(side="left", padx=(0, 8))
            ttk.Combobox(options, textvariable=self.settings["language"], values=("ru", "en"),
                         state="readonly", width=8).pack(side="left")
        self.status = tk.StringVar(value="Выбери клипы из проекта.")
        ttk.Label(panel, textvariable=self.status, wraplength=960).pack(anchor="w", pady=(8, 10))
        footer = ttk.Frame(panel)
        footer.pack(fill="x")
        self.progress = ttk.Progressbar(footer, mode="indeterminate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 18))
        self.result_folder = None
        self.open_result = ttk.Button(footer, text="Результат",
                                     command=lambda: os.startfile(self.result_folder))
        self.open_result.pack(side="right", padx=(8, 0))
        self.open_result.state(["disabled"])
        self.button = ttk.Button(footer, text=action, style="Accent.TButton", command=self.start)
        self.button.pack(side="right")
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.load)

    def setting(self, parent, label, name, value):
        ttk.Label(parent, text=label).pack(side="left", padx=(0, 8))
        variable = tk.StringVar(value=value)
        self.settings[name] = variable
        ttk.Entry(parent, textvariable=variable, width=7).pack(side="left", padx=(0, 18))

    def dot(self, selected):
        image = tk.PhotoImage(master=self.root, width=18, height=18)
        for y in range(18):
            for x in range(18):
                radius = ((x - 8.5) ** 2 + (y - 8.5) ** 2) ** 0.5
                if 5.5 <= radius <= 7:
                    image.put("#ec8b3c" if selected else "#686868", (x, y))
                elif selected and radius < 3.6:
                    image.put("#ec8b3c", (x, y))
        return image

    def selected(self, tree):
        selection = tree.selection()
        if selection:
            self.chosen[tree] = selection[0]
        chosen = self.chosen.get(tree)
        for item in tree.get_children():
            tree.item(item, image=self.dots[int(item == chosen)])
        if chosen is not None and int(chosen) < len(self.clips):
            clip = self.clips[int(chosen)]
            self.selection_labels[tree].set("●  " + clip.name)
        else:
            self.selection_labels[tree].set("Клип не выбран")

    def use_camera_audio(self):
        self.chosen.pop(self.secondary, None)
        self.secondary.selection_remove(self.secondary.selection())
        self.selected(self.secondary)

    def table(self, parent, caption):
        frame = ttk.Frame(parent, padding=(0, 0, 12, 0))
        parent.add(frame, weight=1)
        ttk.Label(frame, text=caption, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        search = tk.StringVar()
        search_row = ttk.Frame(frame)
        search_row.pack(fill="x", pady=(0, 10))
        ttk.Label(search_row, text="Поиск", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        ttk.Entry(search_row, textvariable=search).pack(side="left", fill="x", expand=True)
        wrapper = ttk.Frame(frame)
        wrapper.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrapper, columns=("name", "type", "duration"),
                           show="tree headings", selectmode="browse")
        tree.column("#0", width=34, minwidth=34, stretch=False)
        tree.heading("#0", text="")
        for name, label, width in (("name", "Клип", 270), ("type", "Содержимое", 130),
                                    ("duration", "Длина", 88)):
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=70, stretch=name == "name")
        bar = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.tag_configure("offline", foreground="#d28d88")
        summary = tk.StringVar(value="Клип не выбран")
        ttk.Label(frame, textvariable=summary, style="Selection.TLabel",
                  wraplength=480).pack(anchor="w", pady=(12, 0))
        self.selection_labels[tree] = summary
        self.filters[tree] = search
        search.trace_add("write", lambda *_: self.populate(tree))
        tree.bind("<<TreeviewSelect>>", lambda event: self.selected(tree))
        return tree

    def populate(self, only=None):
        for tree in ((only,) if only is not None else (self.primary, self.secondary)):
            if tree is None:
                continue
            query = self.filters[tree].get().casefold()
            selected = self.chosen.get(tree)
            tree.delete(*tree.get_children())
            for index, clip in enumerate(self.clips):
                allowed = clip.has_audio if tree is self.secondary else (
                    clip.has_video if self.kind != "subtitles" else clip.has_audio)
                if self.kind == "audio-sync" and tree is self.primary:
                    allowed = clip.has_video and clip.has_audio
                if not allowed or query not in (clip.name + " " + clip.folder).casefold():
                    continue
                offline = not clip.path.is_file()
                media_type = "Видео + звук" if clip.has_video and clip.has_audio else (
                    "Видео" if clip.has_video else "Аудио")
                if tree is self.secondary and clip.has_video:
                    media_type = "Звук из видео"
                item = tree.insert("", "end", iid=str(index),
                                   image=self.dots[int(str(index) == selected)],
                                   values=(clip.name + (" · офлайн" if offline else ""),
                                           media_type, clip.duration),
                                   tags=("offline",) if offline else ())
                if str(index) == selected:
                    tree.selection_set(item)
            self.selected(tree)

    def load(self):
        if self.busy:
            return
        try:
            self.resolve, self.project = connect()
            self.project_id = self.project.GetUniqueId()
            self.clips = project_clips(self.project)
            self.chosen.clear()
            self.heading.set(f"Проект «{self.project.GetName()}» · клипов в Media Pool: {len(self.clips)}")
            self.populate()
            selected = [str(i) for i, clip in enumerate(self.clips) if clip.selected]
            video = next((i for i in selected if self.primary.exists(i)), None)
            if video:
                self.primary.selection_set(video)
                self.selected(self.primary)
            if self.secondary:
                candidates = [i for i in selected if i != video and self.secondary.exists(i)]
                audio = next((i for i in candidates if not self.clips[int(i)].has_video),
                             next(iter(candidates), None))
                if audio:
                    self.secondary.selection_set(audio)
                    self.selected(self.secondary)
            self.status.set("Выбери клипы из проекта." if self.clips else "В Media Pool нет исходных клипов.")
            self.button.state(["!disabled"])
        except Exception as exc:
            self.status.set(str(exc))
            self.button.state(["disabled"])

    def start(self):
        if self.busy:
            return
        current = self.resolve.GetProjectManager().GetCurrentProject()
        if not current or current.GetUniqueId() != self.project_id:
            self.load()
            self.status.set("Проект изменился. Выбери клипы из обновлённого списка.")
            return
        selected = self.chosen.get(self.primary)
        sound = self.chosen.get(self.secondary)
        if not selected or (self.kind == "audio-sync" and not sound):
            self.status.set("Выбери видео слева и микрофон справа." if self.secondary
                            else "Выбери клип из списка.")
            return
        source = self.clips[int(selected)]
        audio = self.clips[int(sound)] if sound else None
        if audio and audio.path == source.path:
            self.status.set("Для отдельной записи выбери другой клип справа.")
            return
        if not source.path.is_file() or (audio and not audio.path.is_file()):
            self.status.set("Клип офлайн. Переподключи исходник в Resolve и обнови список.")
            return
        extension = {"audio-sync": ".wav", "autocut": ".fcpxml", "scenes": ".fcpxml",
                     "quality": ".csv", "subtitles": ".srt"}[self.kind]
        destination = output_path(self.project.GetName(), source.path.stem + "-" + self.kind + extension)
        settings = {key: value.get().replace(",", ".") for key, value in self.settings.items()}
        target_folder = self.project.GetMediaPool().GetCurrentFolder()
        results = queue.SimpleQueue()
        self.busy = True
        self.button.state(["disabled"])
        self.refresh.state(["disabled"])
        self.status.set("Обрабатываю выбранные клипы…")
        self.progress.start(12)

        def work():
            try:
                results.put((True, process(self.kind, source.path, audio.path if audio else None,
                                          destination, settings)))
            except Exception as exc:
                results.put((False, str(exc)))

        def poll():
            try:
                ok, message = results.get_nowait()
            except queue.Empty:
                self.root.after(150, poll)
                return
            self.busy = False
            self.progress.stop()
            self.button.state(["!disabled"])
            self.refresh.state(["!disabled"])
            if ok:
                self.result_folder = str(destination.parent)
                self.open_result.state(["!disabled"])
                try:
                    current = self.resolve.GetProjectManager().GetCurrentProject()
                    if not current or current.GetUniqueId() != self.project_id:
                        raise ValueError("Открыт другой проект. Результат сохранён, импорт отложен.")
                    pool = current.GetMediaPool()
                    if extension == ".fcpxml":
                        imported = pool.ImportTimelineFromFile(str(destination))
                    elif extension in (".wav", ".srt"):
                        previous = pool.GetCurrentFolder()
                        try:
                            if not pool.SetCurrentFolder(target_folder):
                                raise ValueError("Папка проекта больше недоступна.")
                            imported = pool.ImportMedia([str(destination)])
                        finally:
                            pool.SetCurrentFolder(previous)
                    else:
                        imported = None
                    if extension != ".csv" and not imported:
                        raise ValueError("Resolve не импортировал результат автоматически.")
                    self.status.set(message + (" Результат добавлен в проект." if imported else "") +
                                    f"\n{destination}")
                except Exception as exc:
                    self.status.set(f"{message}\n{exc}\nФайл сохранён — {destination}")
            else:
                self.status.set("Не удалось обработать клип. " + message)

        threading.Thread(target=work, daemon=True).start()
        self.root.after(150, poll)

    def close(self):
        if self.busy:
            self.status.set("Дождись завершения обработки перед закрытием окна.")
            return
        self.root.destroy()


def gui(kind):
    root = tk.Tk()
    ProjectTool(root, kind)
    root.mainloop()
