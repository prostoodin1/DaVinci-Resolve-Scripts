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
            clips.append(ProjectClip(
                item.GetName(), Path(path), name, kind, properties.get("Duration", ""),
                item.GetMediaId() in chosen, bool(properties.get("Audio Codec", ""))
                or str(properties.get("Audio Ch", "0")) not in ("", "0"),
                bool(properties.get("Video Codec", "")) or "Video" in kind,
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
        root.geometry("1040x700")
        root.minsize(860, 580)
        root.configure(bg="#151719")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", background="#151719", foreground="#eef0f2", font=("Segoe UI", 10))
        style.configure("TFrame", background="#151719")
        style.configure("TLabel", background="#151719")
        style.configure("Title.TLabel", font=("Segoe UI", 24, "bold"))
        style.configure("Muted.TLabel", foreground="#a5adb5")
        style.configure("TButton", padding=(16, 9), background="#30363c", borderwidth=0)
        style.map("TButton", background=[("active", "#404951"), ("disabled", "#24282d")])
        style.configure("Accent.TButton", background="#386ce8", foreground="white")
        style.map("Accent.TButton", background=[("active", "#4b7df1"), ("disabled", "#30363c")])
        style.configure("Treeview", background="#202428", fieldbackground="#202428",
                        foreground="#eef0f2", rowheight=34, borderwidth=0)
        style.map("Treeview", background=[("selected", "#335ca1")],
                  foreground=[("selected", "white")])
        style.configure("Treeview.Heading", background="#2b3036", foreground="#bfc7cf", padding=8)
        style.configure("TEntry", fieldbackground="#252b30", foreground="white", padding=7)
        style.configure("TCombobox", fieldbackground="#252b30", foreground="white", padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", "#252b30")],
                  foreground=[("readonly", "white")])

        panel = ttk.Frame(root, padding=26)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text=title, style="Title.TLabel").pack(anchor="w")
        self.heading = tk.StringVar(value="Подключаюсь к открытому проекту…")
        ttk.Label(panel, textvariable=self.heading, style="Muted.TLabel").pack(anchor="w", pady=(6, 18))
        toolbar = ttk.Frame(panel)
        toolbar.pack(fill="x", pady=(0, 12))
        self.search = tk.StringVar()
        ttk.Label(toolbar, text="Поиск в проекте").pack(side="left", padx=(0, 12))
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search, width=34)
        self.search_entry.pack(side="left")
        self.refresh = ttk.Button(toolbar, text="Обновить проект", command=self.load)
        self.refresh.pack(side="right")
        self.search.trace_add("write", lambda *_: self.populate())
        tables = ttk.Panedwindow(panel, orient="horizontal")
        tables.pack(fill="both", expand=True)
        self.primary = self.table(tables, primary)
        self.secondary = None
        if kind in ("audio-sync", "autocut"):
            caption = "Запись микрофона" if kind == "audio-sync" else "Звук для анализа"
            self.secondary = self.table(tables, caption)
            if kind == "autocut":
                ttk.Label(panel, text="Звук из видео используется, пока справа не выбрана другая запись.",
                          style="Muted.TLabel").pack(anchor="w", pady=(10, 0))
                self.clear_audio = ttk.Button(panel, text="Использовать звук видео",
                    command=lambda: self.secondary.selection_remove(self.secondary.selection()))
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

    def table(self, parent, caption):
        frame = ttk.Frame(parent, padding=(0, 0, 12, 0))
        parent.add(frame, weight=1)
        ttk.Label(frame, text=caption, font=("Segoe UI", 12, "bold")).pack(anchor="w", pady=(0, 8))
        wrapper = ttk.Frame(frame)
        wrapper.pack(fill="both", expand=True)
        tree = ttk.Treeview(wrapper, columns=("name", "duration", "folder"),
                           show="headings", selectmode="browse")
        for name, label, width in (("name", "Клип", 250), ("duration", "Длина", 85),
                                    ("folder", "Папка проекта", 110)):
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=70)
        bar = ttk.Scrollbar(wrapper, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        tree.tag_configure("offline", foreground="#d28d88")
        return tree

    def populate(self):
        query = self.search.get().casefold()
        for tree in (self.primary, self.secondary):
            if tree is None:
                continue
            selected = set(tree.selection())
            tree.delete(*tree.get_children())
            for index, clip in enumerate(self.clips):
                allowed = clip.has_audio if tree is self.secondary else (
                    clip.has_video if self.kind != "subtitles" else clip.has_audio)
                if not allowed or query not in (clip.name + " " + clip.folder).casefold():
                    continue
                offline = not clip.path.is_file()
                item = tree.insert("", "end", iid=str(index),
                                   values=(clip.name + (" · офлайн" if offline else ""),
                                           clip.duration, clip.folder),
                                   tags=("offline",) if offline else ())
                if str(index) in selected:
                    tree.selection_set(item)

    def load(self):
        if self.busy:
            return
        try:
            self.resolve, self.project = connect()
            self.project_id = self.project.GetUniqueId()
            self.clips = project_clips(self.project)
            self.heading.set(f"Проект «{self.project.GetName()}» · клипов в Media Pool: {len(self.clips)}")
            self.populate()
            selected = [str(i) for i, clip in enumerate(self.clips) if clip.selected]
            video = next((i for i in selected if self.primary.exists(i)), None)
            if video:
                self.primary.selection_set(video)
            if self.secondary:
                audio = next((i for i in selected if i != video and self.secondary.exists(i)), None)
                if audio:
                    self.secondary.selection_set(audio)
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
        selected = self.primary.selection()
        sound = self.secondary.selection() if self.secondary else ()
        if not selected or (self.kind == "audio-sync" and not sound):
            self.status.set("Выбери видео слева и микрофон справа." if self.secondary
                            else "Выбери клип из списка.")
            return
        source = self.clips[int(selected[0])]
        audio = self.clips[int(sound[0])] if sound else None
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
