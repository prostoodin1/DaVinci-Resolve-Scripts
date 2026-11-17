"""Windows installer and launcher for the DaVinci Resolve scripts."""

import base64
import hashlib
import json
import os
import queue
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import winreg
import zipfile
from datetime import datetime
from pathlib import Path
from tkinter import StringVar, BooleanVar, Tk, filedialog, messagebox, simpledialog, ttk

REPO = "https://github.com/prostoodin1/DaVinci-Resolve-Scripts.git"
API = "https://api.github.com/repos/prostoodin1/DaVinci-Resolve-Scripts"
APP = Path(os.environ["LOCALAPPDATA"]) / "DaVinci Resolve Scripts"
MENU = (Path(os.environ["APPDATA"]) / "Blackmagic Design" / "DaVinci Resolve" /
        "Support" / "Fusion" / "Scripts" / "Utility")
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
FFMPEG_VERSION = "9.0.2"
FFMPEG_ARCHIVE = f"ffmpeg-{FFMPEG_VERSION}-essentials_build"
FFMPEG_SHA256 = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
SPEECH_ARCHIVE = "russian-speech.zip"
SPEECH_FILES = (
    "feat.params", "feature_transform", "mdef", "means", "mixture_weights",
    "noisedict", "ru.dic", "ru.lm.bin", "transition_matrices", "variances",
    "LICENSE", "README",
)
ID = re.compile(r"[a-z][a-z0-9-]{0,40}")
VERSION = re.compile(r"\d{4}-\d{8}")
FILENAME = re.compile(r"resolve_[a-z_]+\.py")


def find_resolve():
    locations = [Path(os.environ["ProgramFiles"]) / "Blackmagic Design" / "DaVinci Resolve"]
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                                    0, winreg.KEY_READ | view) as installed:
                    for index in range(winreg.QueryInfoKey(installed)[0]):
                        try:
                            with winreg.OpenKey(installed, winreg.EnumKey(installed, index)) as key:
                                name = winreg.QueryValueEx(key, "DisplayName")[0]
                                if name.startswith("DaVinci Resolve"):
                                    locations.append(Path(winreg.QueryValueEx(key, "InstallLocation")[0]))
                        except OSError:
                            continue
            except OSError:
                continue
    return next((folder / "Resolve.exe" for folder in locations
                 if (folder / "Resolve.exe").is_file()), None)


def load_catalog(folder):
    catalog = json.loads((folder / "catalog.json").read_text(encoding="utf-8"))
    if catalog.get("schema") not in (1, 2):
        raise ValueError("Обнови установщик для нового формата каталога.")
    seen_ids = set()
    seen_menus = set()
    for item in catalog["scripts"]:
        identifier, version, menu = item["id"], item["version"], item["menu"]
        files = item["files"]
        if (not ID.fullmatch(identifier) or not VERSION.fullmatch(version)
                or not re.fullmatch(r"DRS - [A-Za-z0-9 -]{1,64}", menu)
                or identifier in seen_ids or menu.lower() in seen_menus
                or item["entry"] not in files or len(files) != len(set(files))
                or any(not FILENAME.fullmatch(name) or not (folder / name).is_file()
                       for name in files)):
            raise ValueError(f"Некорректная запись каталога {identifier}.")
        datetime.strptime(version[5:], "%d%m%Y")
        seen_ids.add(identifier)
        seen_menus.add(menu.lower())
    return catalog["scripts"]


def installed_version(identifier, app=APP):
    marker = app / "scripts" / identifier / "installed.txt"
    if marker.is_file():
        version = marker.read_text(encoding="ascii").strip()
        if VERSION.fullmatch(version):
            return version
    return None


def atomic_write(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_copy(path, source):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            shutil.copyfileobj(source, handle)
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def setup_ffmpeg(app=APP, progress=lambda text: None):
    destination = app / "FFmpeg" / "bin"
    names = ("ffmpeg.exe", "ffprobe.exe")
    if all((destination / name).is_file() for name in names):
        return destination
    archive = BUNDLE / "ffmpeg.zip"
    if not archive.is_file():
        raise ValueError("FFmpeg отсутствует. Запусти полный ResolveScriptsSetup.exe для восстановления.")
    progress("Распаковываю FFmpeg из установщика…")
    with archive.open("rb") as handle:
        if hashlib.file_digest(handle, "sha256").hexdigest() != FFMPEG_SHA256:
            raise ValueError("Архив FFmpeg в установщике не прошёл проверку SHA-256.")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        for name in ("LICENSE", "README.txt"):
            atomic_write(destination.parent / name, package.read(f"{FFMPEG_ARCHIVE}/{name}"))
        for name in names:
            with package.open(f"{FFMPEG_ARCHIVE}/bin/{name}") as binary:
                atomic_copy(destination / name, binary)
    return destination


def setup_speech(app, requested, progress):
    archive = app / SPEECH_ARCHIVE
    bundled = BUNDLE / SPEECH_ARCHIVE
    destination = app / "SpeechRussian"
    if not archive.is_file() and bundled.is_file():
        progress("Сохраняю русский речевой пакет для офлайн-установки…")
        with bundled.open("rb") as source:
            atomic_copy(archive, source)
    if not requested or all((destination / name).is_file() for name in SPEECH_FILES):
        return
    if not archive.is_file():
        raise ValueError("Нет русского речевого пакета. Запусти полный ResolveScriptsSetup.exe.")
    progress("Распаковываю русский речевой пакет…")
    destination.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as model:
        for name in SPEECH_FILES:
            with model.open(name) as source:
                atomic_copy(destination / name, source)


def find_git():
    installed = Path(os.environ["ProgramFiles"]) / "Git" / "cmd" / "git.exe"
    return shutil.which("git") or (str(installed) if installed.is_file() else None)


def fetch_catalog(destination, token=None):
    if token:
        return fetch_catalog_api(destination, token)
    git = find_git()
    if not git:
        return fetch_catalog_api(destination, None)
    result = subprocess.run(
        [git, "clone", "--quiet", "--depth", "1", "--branch", "main", REPO, str(destination)],
        capture_output=True, encoding="utf-8", errors="replace",
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise ValueError("GitHub недоступен через сохранённый вход. Используй «Войти по токену» "
                         "или проверь вход в Git Credential Manager.")
    return load_catalog(destination)


def fetch_catalog_api(destination, token):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "DaVinci-Resolve-Scripts"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def api(path):
        request = urllib.request.Request(API + path, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403, 404):
                raise ValueError("Для приватного каталога нажми «Войти по токену» и укажи "
                                 "токен GitHub с доступом Contents: Read к репозиторию.") from None
            raise

    revision = api("/commits/main")["sha"]
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("GitHub вернул неверный идентификатор коммита.")

    def contents(filename):
        item = api(f"/contents/{urllib.parse.quote(filename)}?ref={revision}")
        if item["encoding"] != "base64":
            raise ValueError(f"Неподдерживаемый формат файла {filename}.")
        return base64.b64decode(item["content"])

    catalog_file = contents("catalog.json")
    catalog = json.loads(catalog_file)
    if catalog.get("schema") not in (1, 2):
        raise ValueError("Обнови установщик для нового формата каталога.")
    names = {name for item in catalog["scripts"] for name in item["files"]}
    if any(not FILENAME.fullmatch(name) for name in names):
        raise ValueError("В каталоге GitHub указан неверный путь к скрипту.")
    (destination / "catalog.json").write_bytes(catalog_file)
    for name in names:
        (destination / name).write_bytes(contents(name))
    return load_catalog(destination)


def install(selected, folder, launcher, app=APP, menu=MENU, progress=lambda text: None):
    app.mkdir(parents=True, exist_ok=True)
    menu.mkdir(parents=True, exist_ok=True)
    setup_ffmpeg(app, progress)
    setup_speech(app, any(item["id"] == "subtitles" for item in selected), progress)
    persistent_launcher = app / "ResolveScripts.exe"
    if launcher.resolve() != persistent_launcher.resolve():
        if not persistent_launcher.exists() or persistent_launcher.read_bytes() != launcher.read_bytes():
            atomic_write(persistent_launcher, launcher.read_bytes())
    command = r'start "" "%LOCALAPPDATA%\DaVinci Resolve Scripts\ResolveScripts.exe"'
    atomic_write(menu / "DRS - Manage scripts.lua", f"os.execute([=[{command}]=])\n".encode("ascii"))

    for item in selected:
        identifier, version = item["id"], item["version"]
        previous = installed_version(identifier, app)
        if previous and previous > version:
            continue
        progress(f"Устанавливаю {item['name']} {version}…")
        script_root = app / "scripts" / identifier
        script_root.mkdir(parents=True, exist_ok=True)
        destination = script_root / version
        files = {name: (folder / name).read_bytes() for name in item["files"]}
        for name, content in files.items():
            compile(content, name, "exec")
        files["entry.txt"] = item["entry"].encode("ascii")
        if not destination.exists():
            with tempfile.TemporaryDirectory(dir=script_root) as staging:
                payload = Path(staging) / "payload"
                payload.mkdir()
                for name, content in files.items():
                    (payload / name).write_bytes(content)
                payload.rename(destination)
        else:
            for name, content in files.items():
                target = destination / name
                if not target.is_file():
                    atomic_write(target, content)
                elif target.read_bytes() != content:
                    raise ValueError(f"{identifier} {version}: файлы изменились, а версия осталась прежней.")
        # CMD expands the fixed environment variable when Resolve launches the Lua entry.
        command = (r'start "" "%LOCALAPPDATA%\DaVinci Resolve Scripts\ResolveScripts.exe"'
                   f" --run {identifier} {version}")
        wrapper = f"-- {version}\nos.execute([=[{command}]=])\n"
        atomic_write(menu / f"{item['menu']}.lua", wrapper.encode("ascii"))
        atomic_write(script_root / "installed.txt", (version + "\n").encode("ascii"))


def launch(identifier, version, arguments=()):
    if not ID.fullmatch(identifier) or not VERSION.fullmatch(version):
        raise ValueError("Некорректный идентификатор или версия скрипта.")
    folder = APP / "scripts" / identifier / version
    entry = (folder / "entry.txt").read_text(encoding="ascii").strip()
    if not FILENAME.fullmatch(entry):
        raise ValueError("Некорректный файл запуска.")
    ffmpeg = APP / "FFmpeg" / "bin"
    if ffmpeg.is_dir():
        os.environ["FFMPEG_DIR"] = str(ffmpeg)
    speech = APP / "SpeechRussian"
    if speech.is_dir():
        os.environ["DRS_SPEECH_RU"] = str(speech)
    sys.path.insert(0, str(folder))
    sys.argv = [str(folder / entry), *arguments]
    runpy.run_path(sys.argv[0], run_name="__main__")


def gui():
    root = Tk()
    root.title("Скрипты DaVinci Resolve")
    root.minsize(600, 280)
    panel = ttk.Frame(root, padding=16)
    panel.pack(fill="both", expand=True)
    resolve = StringVar(value=str(find_resolve() or ""))
    status = StringVar(value="Выбери скрипты для установки или проверь обновления.")
    choices = ttk.Frame(panel)
    folder = BUNDLE
    rows = []
    temporary = tempfile.TemporaryDirectory(prefix="resolve-scripts-")

    def choose():
        selected = filedialog.askopenfilename(
            parent=root, title="Найди Resolve.exe", filetypes=[("DaVinci Resolve", "Resolve.exe")],
        )
        if selected:
            resolve.set(selected)

    def render(items, selected=None):
        for widget in choices.winfo_children():
            widget.destroy()
        rows.clear()
        for item in items:
            identifier, available = item["id"], item["version"]
            current = installed_version(identifier)
            choice = BooleanVar(value=identifier in selected if selected is not None else current is not None)
            label = f"{item['name']}  ·  {available}"
            if current and current != available:
                label += f"  ·  установлено {current}"
            elif current:
                label += "  ·  установлено"
            ttk.Checkbutton(choices, text=label, variable=choice).pack(anchor="w", pady=3)
            rows.append((item, choice))

    def background(job, success):
        results = queue.SimpleQueue()
        for button in (apply_button, update_button, choose_button, token_button):
            button.state(["disabled"])

        def execute():
            try:
                result = job(lambda message: results.put(("progress", message)))
            except Exception as exc:
                results.put(("error", str(exc)))
            else:
                results.put(("done", result))

        def poll():
            try:
                state, value = results.get_nowait()
            except queue.Empty:
                root.after(120, poll)
                return
            if state == "progress":
                status.set(value)
                root.after(1, poll)
                return
            for button in (apply_button, update_button, choose_button, token_button):
                button.state(["!disabled"])
            if state == "error":
                status.set("Операция прервана.")
                messagebox.showerror("Скрипты DaVinci Resolve", value, parent=root)
            else:
                success(value)

        threading.Thread(target=execute, daemon=True).start()
        root.after(120, poll)

    token = None

    def enter_token():
        nonlocal token
        value = simpledialog.askstring(
            "Доступ к приватному GitHub",
            "Токен GitHub с доступом Contents: Read к репозиторию.\n"
            "Токен используется до закрытия этого окна.",
            show="*", parent=root,
        )
        if value and value.strip():
            token = value.strip()
            check()

    def check():
        selected = {item["id"] for item, choice in rows if choice.get()}
        selected.update(item["id"] for item, _ in rows if installed_version(item["id"]))

        def download(progress):
            progress("Проверяю каталог в GitHub…")
            path = Path(tempfile.mkdtemp(dir=temporary.name))
            return path, fetch_catalog(path, token)

        def loaded(result):
            nonlocal folder
            folder, items = result
            render(items, selected)
            updates = sum(1 for item in items if (current := installed_version(item["id"]))
                          and current < item["version"])
            status.set(f"Каталог GitHub загружен. Обновлений установленных скриптов: {updates}.")

        background(download, loaded)

    def apply():
        target = Path(resolve.get())
        if target.name.lower() != "resolve.exe" or not target.is_file():
            messagebox.showerror("DaVinci Resolve", "Укажи путь до Resolve.exe.", parent=root)
            return
        selected = [item for item, choice in rows if choice.get()]
        if not selected:
            messagebox.showinfo("Скрипты", "Отметь хотя бы один скрипт.", parent=root)
            return
        if not getattr(sys, "frozen", False):
            messagebox.showerror("Запуск", "Для установки запусти собранный ResolveScriptsSetup.exe.", parent=root)
            return

        def complete(_):
            render([item for item, _ in rows], {item["id"] for item in selected})
            status.set("Готово. Перезапусти Resolve и открой Workspace → Scripts.")

        runtime = BUNDLE / "ResolveScriptsRuntime.exe"
        launcher = runtime if runtime.is_file() else Path(sys.executable)
        background(lambda progress: install(selected, folder, launcher, progress=progress), complete)

    ttk.Label(panel, text="DaVinci Resolve").pack(anchor="w")
    path_row = ttk.Frame(panel)
    path_row.pack(fill="x", pady=(4, 12))
    ttk.Entry(path_row, textvariable=resolve, state="readonly").pack(side="left", fill="x", expand=True)
    choose_button = ttk.Button(path_row, text="Найти вручную", command=choose)
    choose_button.pack(side="right", padx=(8, 0))
    choices.pack(fill="both", expand=True)
    actions = ttk.Frame(panel)
    actions.pack(fill="x", pady=(12, 8))
    update_button = ttk.Button(actions, text="Проверить GitHub", command=check)
    update_button.pack(side="left")
    token_button = ttk.Button(actions, text="Войти по токену", command=enter_token)
    token_button.pack(side="left", padx=(6, 0))
    apply_button = ttk.Button(actions, text="Установить / обновить выбранные", command=apply)
    apply_button.pack(side="right")
    ttk.Label(panel, textvariable=status, wraplength=580).pack(anchor="w")
    render(load_catalog(folder))
    try:
        root.mainloop()
    finally:
        temporary.cleanup()


if __name__ == "__main__":
    try:
        if len(sys.argv) >= 4 and sys.argv[1] == "--run":
            launch(sys.argv[2], sys.argv[3], sys.argv[4:])
        else:
            gui()
    except Exception as error:
        messagebox.showerror("Скрипты DaVinci Resolve", str(error))
