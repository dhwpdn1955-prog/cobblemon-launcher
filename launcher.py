import os, sys, json, hashlib, zipfile, shutil, subprocess, threading, urllib.request
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path

GITHUB_USER   = "dhwpdn1955-prog"
GITHUB_REPO   = "cobblemon-launcher"
INSTANCE_NAME = "Cobbleverse"

VERSION_URL = f"https://raw.githubusercontent.com/{GITHUB_USER}/{GITHUB_REPO}/main/version.json"
PRISM_BASE   = Path(os.environ.get("APPDATA", "")) / "PrismLauncher" / "instances"
INSTANCE_DIR = PRISM_BASE / INSTANCE_NAME / ".minecraft"
MODS_DIR          = INSTANCE_DIR / "mods"
RESOURCEPACKS_DIR = INSTANCE_DIR / "resourcepacks"
DATAPACKS_DIR     = INSTANCE_DIR / "datapacks"
META_FILE    = INSTANCE_DIR / "launcher_meta.json"
LAUNCHER_VERSION = "1.1.0"

# overrides 하위 폴더별 동기화 전략
# True = 런처가 완전 관리 (없는 파일 삭제), False = 덮어쓰기만
MANAGED_DIRS = {
    "mods":         True,
    "resourcepacks": True,
    "datapacks":    True,
    "config":       False,  # config는 삭제 없이 덮어쓰기만
}

def sha1_of_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "CobblemonLauncher/1.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())

def download_file(url, dest, progress_cb=None):
    req = urllib.request.Request(url, headers={"User-Agent": "CobblemonLauncher/1.1"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total:
                    progress_cb(downloaded / total)

def load_local_meta():
    if META_FILE.exists():
        with open(META_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": None}

def save_local_meta(meta):
    META_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(META_FILE, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

def parse_mrpack(mrpack_path):
    with zipfile.ZipFile(mrpack_path, "r") as zf:
        with zf.open("modrinth.index.json") as f:
            return json.load(f)

def sync_mods(mrpack_path, log_cb, progress_cb):
    """modrinth.index.json 기반 모드 동기화 (해시 비교)"""
    index = parse_mrpack(mrpack_path)
    remote_mods = {
        Path(e["path"]).name: e
        for e in index["files"]
        if e["path"].startswith("mods/")
    }
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    local_files = {p.name for p in MODS_DIR.glob("*.jar")}

    for name in local_files - set(remote_mods.keys()):
        (MODS_DIR / name).unlink()
        log_cb(f"  [삭제] {name}")

    to_download = []
    for name, info in remote_mods.items():
        dest = MODS_DIR / name
        if dest.exists() and sha1_of_file(dest) == info["hashes"]["sha1"]:
            continue
        to_download.append((name, info))

    if not to_download:
        log_cb("  모드: 모두 최신 상태")
        progress_cb(1.0)
        return

    log_cb(f"  모드: {len(to_download)}개 업데이트")
    for i, (name, info) in enumerate(to_download):
        log_cb(f"  [{i+1}/{len(to_download)}] {name}")
        download_file(info["downloads"][0], MODS_DIR / name)
        progress_cb((i + 1) / len(to_download))

def sync_overrides(mrpack_path, log_cb):
    """
    overrides 폴더 동기화
    - mods/resourcepacks/datapacks: 완전 관리 (원격에 없는 파일 삭제)
    - config 등 나머지: 덮어쓰기만
    """
    with zipfile.ZipFile(mrpack_path, "r") as zf:
        all_entries = [n for n in zf.namelist() if n.startswith("overrides/")]
        if not all_entries:
            return

        # 원격 파일 목록을 폴더별로 수집
        remote_by_folder = {}
        for entry in all_entries:
            rel = entry[len("overrides/"):]
            if not rel or entry.endswith("/"):
                continue
            parts = Path(rel).parts
            if not parts:
                continue
            folder = parts[0]
            remote_by_folder.setdefault(folder, set()).add(Path(rel).name)

        # 완전 관리 폴더: 원격에 없는 로컬 파일 삭제
        for folder, managed in MANAGED_DIRS.items():
            if not managed:
                continue
            local_dir = INSTANCE_DIR / folder
            if not local_dir.exists():
                continue
            remote_files = remote_by_folder.get(folder, set())
            for local_file in local_dir.iterdir():
                if local_file.is_file() and local_file.name not in remote_files:
                    local_file.unlink()
                    log_cb(f"  [삭제] {folder}/{local_file.name}")

        # 파일 복사
        changed = []
        for entry in all_entries:
            rel = entry[len("overrides/"):]
            if not rel or entry.endswith("/"):
                continue
            dest = INSTANCE_DIR / rel
            if entry.endswith("/"):
                dest.mkdir(parents=True, exist_ok=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src:
                data = src.read()
            # 변경된 경우만 덮어쓰기
            if dest.exists() and dest.read_bytes() == data:
                continue
            dest.write_bytes(data)
            changed.append(rel)

        if changed:
            log_cb(f"  설정/리소스: {len(changed)}개 업데이트")
        else:
            log_cb("  설정/리소스: 모두 최신 상태")

def find_prism():
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "PrismLauncher" / "prismlauncher.exe",
        Path("C:/Program Files/PrismLauncher/prismlauncher.exe"),
        Path("C:/Program Files (x86)/PrismLauncher/prismlauncher.exe"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return None

BG = "#0e0e0e"; CARD = "#161616"; ACCENT = "#e84040"; TEXT = "#e0e0e0"; DIM = "#666666"

class LauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Cobblemon Self-Made Launcher")
        self.geometry("640x500")
        self.resizable(False, False)
        self.configure(bg=BG)
        self._updating = False
        self._build_ui()
        self.after(400, self._check_versions)

    def _build_ui(self):
        tk.Frame(self, bg=ACCENT, height=3).pack(fill="x")
        tf = tk.Frame(self, bg=BG, pady=18)
        tf.pack(fill="x")
        tk.Label(tf, text="COBBLEMON", font=("Courier New", 28, "bold"), bg=BG, fg=ACCENT).pack()
        tk.Label(tf, text="S E L F - M A D E", font=("Courier New", 9), bg=BG, fg=DIM).pack()

        vf = tk.Frame(self, bg=CARD, padx=24, pady=10)
        vf.pack(fill="x", padx=28)
        self.lbl_local  = tk.Label(vf, text="로컬 버전:  —", font=("Courier New", 10), bg=CARD, fg=DIM, anchor="w")
        self.lbl_local.pack(fill="x")
        self.lbl_remote = tk.Label(vf, text="최신 버전:  —", font=("Courier New", 10), bg=CARD, fg=DIM, anchor="w")
        self.lbl_remote.pack(fill="x")

        lf = tk.Frame(self, bg=BG, padx=28, pady=10)
        lf.pack(fill="both", expand=True)
        self.log_box = tk.Text(lf, height=11, bg="#111", fg="#999",
                               font=("Courier New", 9), relief="flat", bd=0,
                               state="disabled", wrap="word")
        self.log_box.pack(fill="both", expand=True)

        bf = tk.Frame(self, bg=BG, padx=28)
        bf.pack(fill="x", pady=(0, 4))
        s = ttk.Style(); s.theme_use("default")
        s.configure("R.Horizontal.TProgressbar", troughcolor="#1a1a1a", background=ACCENT, thickness=3)
        self.progress = ttk.Progressbar(bf, style="R.Horizontal.TProgressbar", orient="horizontal", mode="determinate")
        self.progress.pack(fill="x")

        btf = tk.Frame(self, bg=BG, pady=12, padx=28)
        btf.pack(fill="x")
        self.btn_update = tk.Button(btf, text="업데이트 확인",
            font=("Courier New", 11, "bold"), bg=ACCENT, fg="white",
            relief="flat", bd=0, activebackground="#c03030", activeforeground="white",
            padx=22, pady=9, cursor="hand2", command=self._on_update)
        self.btn_update.pack(side="left")
        self.btn_launch = tk.Button(btf, text="Prism Launcher 열기",
            font=("Courier New", 10), bg=CARD, fg=TEXT,
            relief="flat", bd=0, activebackground="#222", activeforeground="white",
            padx=16, pady=9, cursor="hand2", command=self._on_launch)
        self.btn_launch.pack(side="left", padx=(10, 0))
        tk.Label(btf, text=f"launcher v{LAUNCHER_VERSION}", font=("Courier New", 8), bg=BG, fg=DIM).pack(side="right")

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def set_progress(self, val):
        self.progress["value"] = val * 100
        self.update_idletasks()

    def _check_versions(self):
        meta = load_local_meta()
        lv = meta.get("version") or "미설치"
        self.lbl_local.configure(text=f"로컬 버전:  {lv}", fg=TEXT)
        def task():
            try:
                remote = fetch_json(VERSION_URL)
                rv = remote.get("version", "?")
                self.lbl_remote.configure(text=f"최신 버전:  {rv}", fg=TEXT)
                if lv != rv:
                    self.log(f"업데이트 가능합니다: {lv}  →  {rv}")
                    self.btn_update.configure(text="▼  업데이트")
                else:
                    self.log("최신 버전이 설치되어 있습니다.")
                    self.btn_update.configure(text="✓  재설치")
            except Exception as e:
                self.lbl_remote.configure(text="최신 버전:  연결 실패", fg=ACCENT)
                self.log(f"버전 확인 실패: {e}")
        threading.Thread(target=task, daemon=True).start()

    def _on_update(self):
        if self._updating:
            return
        self._updating = True
        self.btn_update.configure(state="disabled")
        threading.Thread(target=self._run_update, daemon=True).start()

    def _run_update(self):
        try:
            self.log("\n── 업데이트 시작 ──────────────────────")
            self.set_progress(0)
            self.log("버전 정보 가져오는 중...")
            remote = fetch_json(VERSION_URL)
            rv = remote["version"]
            url = remote["mrpack_url"]
            tmp = Path(os.environ.get("TEMP", ".")) / "cobblemon_update.mrpack"
            self.log(f"모드팩 다운로드 중 (v{rv})...")
            download_file(url, tmp, lambda p: self.set_progress(p * 0.25))
            self.log("\n모드 동기화:")
            sync_mods(tmp, self.log, lambda p: self.set_progress(0.25 + p * 0.65))
            self.log("\n리소스팩/데이터팩/설정 동기화:")
            sync_overrides(tmp, self.log)
            self.set_progress(1.0)
            save_local_meta({"version": rv})
            self.lbl_local.configure(text=f"로컬 버전:  {rv}", fg=TEXT)
            self.log("\n✓ 업데이트 완료!")
            tmp.unlink(missing_ok=True)
        except Exception as e:
            self.log(f"\n✗ 오류: {e}")
            messagebox.showerror("업데이트 오류", str(e))
        finally:
            self._updating = False
            self.btn_update.configure(state="normal")

    def _on_launch(self):
        prism = find_prism()
        if prism:
            subprocess.Popen([str(prism)])
            self.log("Prism Launcher 실행됨")
        else:
            messagebox.showwarning("Prism Launcher 없음",
                "Prism Launcher를 찾을 수 없습니다.\nhttps://prismlauncher.org 에서 설치해주세요.")

if __name__ == "__main__":
    app = LauncherApp()
    app.mainloop()
