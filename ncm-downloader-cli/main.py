#!/usr/bin/env python3
"""
基于 NeteaseCloudMusicApi Enhanced 的命令行工具。

用法:
  1. 部署 NeteaseCloudMusicApi Enhanced:
     https://neteasecloudmusicapienhanced.js.org/
  2. uv run main.py
"""

import os
import re
import sys
import json
import time
import hashlib
import requests
from pathlib import Path
from datetime import datetime

try:
    from rich.console import Console
    from rich.table import Table
    from rich.progress import (
        Progress, BarColumn, DownloadColumn,
        TransferSpeedColumn, TimeRemainingColumn, TextColumn, TaskID,
    )
    from rich.text import Text
    from rich.panel import Panel
    from rich.theme import Theme
except ImportError:
    print("[错误] 需要 rich 库: pip install rich")
    sys.exit(1)

# ─────────────────────────── 常量 ────────────────────────────────────

DEFAULT_API_BASE = "http://localhost:3000"
DEFAULT_DOWNLOAD_DIR = "./downloads"
DEFAULT_QUALITY = "exhigh"
DEFAULT_FILENAME_TEMPLATE = "{artist} - {title}"
DEFAULT_MAX_ARTISTS = 3
DEFAULT_ARTIST_SEPARATOR = ", "
MAX_FILENAME_BYTES = 200

SETTINGS_FILE = Path("netease_settings.json")

QUALITY_LEVELS = {
    "1": ("standard", "标准 (128kbps)"),
    "2": ("higher", "较高 (192kbps)"),
    "3": ("exhigh", "极高 (320kbps)"),
    "4": ("lossless", "无损 (FLAC)"),
    "5": ("hires", "Hi-Res"),
    "6": ("jyeffect", "高清环绕声"),
    "7": ("sky", "沉浸环绕声"),
    "8": ("jymaster", "超清母带"),
}

QUALITY_ORDER = [
    "jymaster", "sky", "jyeffect", "hires",
    "lossless", "exhigh", "higher", "standard",
]

QUALITY_NAMES = {
    "standard": "标准", "higher": "较高", "exhigh": "极高",
    "lossless": "无损", "hires": "Hi-Res", "jyeffect": "高清环绕声",
    "sky": "沉浸环绕声", "jymaster": "超清母带",
}

SEARCH_TYPES = {
    "1": (1, "单曲"), "2": (10, "专辑"), "3": (100, "歌手"),
    "4": (1000, "歌单"), "5": (1002, "用户"), "6": (1004, "MV"),
}

FILENAME_TEMPLATES = {
    "1": ("{artist} - {title}", "歌手 - 歌名"),
    "2": ("{title} - {artist}", "歌名 - 歌手"),
    "3": ("{title}", "仅歌名"),
    "4": ("{artist} - {album} - {title}", "歌手 - 专辑 - 歌名"),
    "5": ("{album}/{artist} - {title}", "专辑文件夹/歌手 - 歌名"),
    "6": ("{artist}/{album}/{title}", "歌手/专辑/歌名"),
}

# ─────────────────────────── 控制台 ──────────────────────────────────

theme = Theme({
    "info": "bold blue",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "dim": "dim",
    "q_normal": "cyan",
    "q_fallback": "bold yellow",
    "title": "bold",
    "hint": "dim italic",
})

console = Console(theme=theme)

def msg_info(text: str):
    console.print(f"  [info]\\[信息][/info] {text}")

def msg_ok(text: str):
    console.print(f"  [ok]\\[完成][/ok] {text}")

def msg_warn(text: str):
    console.print(f"  [warn]\\[警告][/warn] {text}")

def msg_error(text: str):
    console.print(f"  [err]\\[错误][/err] {text}")

def msg_skip(text: str):
    console.print(f"  [dim]\\[跳过][/dim] {text}")

def msg_done(text: str):
    console.print(f"  [ok]\\[完成][/ok] {text}")

def msg_fail(text: str):
    console.print(f"  [err]\\[失败][/err] {text}")


# ─────────────────────────── 设置持久化 ──────────────────────────────

class Settings:
    DEFAULTS = {
        "api_base": DEFAULT_API_BASE,
        "download_dir": DEFAULT_DOWNLOAD_DIR,
        "quality": DEFAULT_QUALITY,
        "filename_template": DEFAULT_FILENAME_TEMPLATE,
        "max_artists": DEFAULT_MAX_ARTISTS,
        "artist_separator": DEFAULT_ARTIST_SEPARATOR,
    }

    def __init__(self, path: Path = SETTINGS_FILE):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self):
        loaded = {}
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                pass
        self.data = {k: loaded.get(k, v) for k, v in self.DEFAULTS.items()}

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            msg_warn(f"保存设置失败: {e}")

    def get(self, key: str):
        return self.data.get(key, self.DEFAULTS.get(key))

    def set(self, key: str, value):
        self.data[key] = value
        self.save()


# ─────────────────────────── 工具函数 ────────────────────────────────

def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip().rstrip('.')


def truncate_bytes(name: str, limit: int) -> str:
    if len(name.encode('utf-8')) <= limit:
        return name
    while len(name.encode('utf-8')) > limit:
        name = name[:-1]
    return name.rstrip()


def fmt_artists(artists: list[str], max_show: int, sep: str = " / ") -> str:
    if not artists:
        return "未知歌手"
    if len(artists) <= max_show:
        return sep.join(artists)
    return sep.join(artists[:max_show]) + f" 等{len(artists)}位歌手"


def build_filename(template: str, title: str, artist_str: str, album: str,
                   song_id: int, ext: str) -> str:
    filled = template.format(
        title=sanitize_filename(title) or str(song_id),
        artist=sanitize_filename(artist_str) or "未知歌手",
        album=sanitize_filename(album) or "未知专辑",
        id=str(song_id),
    )
    parts = [p.strip() for p in filled.replace("\\", "/").split("/") if p.strip()]
    if not parts:
        parts = [str(song_id)]
    safe = [truncate_bytes(sanitize_filename(p), MAX_FILENAME_BYTES) for p in parts]
    safe[-1] = f"{safe[-1]}.{ext}"
    return os.path.join(*safe)


def fmt_bitrate(br: int) -> str:
    if not br:
        return ""
    return f"{br // 1000}kbps" if br >= 1000 else f"{br}kbps"


def guess_quality(br: int, ftype: str) -> str:
    ft = (ftype or "").lower()
    if ft == "flac":
        return "hires" if br and br > 1500000 else "lossless"
    if br:
        if br >= 320000:
            return "exhigh"
        elif br >= 192000:
            return "higher"
    return "standard"


def quality_label(level: str, br: int) -> str:
    name = QUALITY_NAMES.get(level, level)
    bs = fmt_bitrate(br)
    return f"{name} {bs}" if bs else name


def resolve_quality(requested: str, info: dict) -> tuple[str, bool]:
    """返回 (显示字符串, 是否回落)"""
    actual = info.get("level", "") or guess_quality(info.get("br", 0), info.get("type", ""))
    br = info.get("br", 0)
    label = quality_label(actual, br)
    if actual == requested:
        return label, False
    try:
        rr = QUALITY_ORDER.index(requested)
        ar = QUALITY_ORDER.index(actual)
    except ValueError:
        return f"{QUALITY_NAMES.get(requested, requested)} -> {label}", True
    if ar > rr:
        return f"{QUALITY_NAMES.get(requested, requested)} -> {label}", True
    return label, False


def extract_meta(song: dict) -> tuple[str, list[str], str]:
    name = song.get("name", "")
    ar = song.get("ar", song.get("artists", []))
    artists = [a.get("name", "") for a in ar if a.get("name")]
    album = song.get("al", song.get("album", {})).get("name", "")
    return name, artists, album


def fmt_duration(ms: int) -> str:
    if not ms:
        return "--:--"
    return f"{ms // 60000}:{(ms % 60000) // 1000:02d}"


# ─────────────────────────── API 客户端 ──────────────────────────────

class NeteaseAPI:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.cookie_str = ""
        self.logged_in = False
        self.user_info = {}
        self.cookie_file = Path("netease_cookie.json")
        self._load_cookie()

    def _req(self, ep: str, params: dict = None, method: str = "GET") -> dict:
        url = f"{self.base_url}{ep}"
        params = params or {}
        if self.cookie_str:
            params["cookie"] = self.cookie_str
        params["timestamp"] = int(time.time() * 1000)
        try:
            if method.upper() == "POST":
                r = self.session.post(url, data=params, timeout=30)
            else:
                r = self.session.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.ConnectionError:
            msg_error(f"无法连接到 API 服务器 ({self.base_url})")
            return {"code": -1}
        except requests.exceptions.Timeout:
            msg_error("请求超时")
            return {"code": -1}
        except Exception as e:
            msg_error(f"请求失败: {e}")
            return {"code": -1}

    def _save_cookie(self):
        d = {"cookie": self.cookie_str, "user_info": self.user_info, "logged_in": self.logged_in}
        self.cookie_file.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")

    def _load_cookie(self):
        if self.cookie_file.exists():
            try:
                d = json.loads(self.cookie_file.read_text(encoding="utf-8"))
                self.cookie_str = d.get("cookie", "")
                self.user_info = d.get("user_info", {})
                self.logged_in = d.get("logged_in", False)
            except Exception:
                pass

    # 登录
    def login_phone(self, phone, pw, cc="86"):
        md5 = hashlib.md5(pw.encode()).hexdigest()
        r = self._req("/login/cellphone", {"phone": phone, "md5_password": md5, "countrycode": cc})
        if r.get("code") == 200:
            self.cookie_str, self.user_info, self.logged_in = r.get("cookie", ""), r.get("profile", {}), True
            self._save_cookie()
        return r

    def login_email(self, email, pw):
        md5 = hashlib.md5(pw.encode()).hexdigest()
        r = self._req("/login", {"email": email, "md5_password": md5})
        if r.get("code") == 200:
            self.cookie_str, self.user_info, self.logged_in = r.get("cookie", ""), r.get("profile", {}), True
            self._save_cookie()
        return r

    def login_qr_key(self):
        return self._req("/login/qr/key")

    def login_qr_create(self, key):
        return self._req("/login/qr/create", {"key": key, "qrimg": "true"})

    def login_qr_check(self, key):
        return self._req("/login/qr/check", {"key": key})

    def login_status(self):
        return self._req("/login/status")

    def logout(self):
        r = self._req("/logout")
        self.cookie_str, self.user_info, self.logged_in = "", {}, False
        if self.cookie_file.exists():
            self.cookie_file.unlink()
        return r

    def login_refresh(self):
        return self._req("/login/refresh")

    # 搜索
    def search(self, kw, stype=1, limit=30, offset=0):
        return self._req("/cloudsearch", {"keywords": kw, "type": stype, "limit": limit, "offset": offset})

    def search_hot(self):
        return self._req("/search/hot/detail")

    # 歌曲
    def song_detail(self, ids):
        return self._req("/song/detail", {"ids": ",".join(str(i) for i in ids)})

    def song_url(self, ids, level="exhigh"):
        return self._req("/song/url/v1", {"id": ",".join(str(i) for i in ids), "level": level})

    def lyric(self, sid):
        return self._req("/lyric", {"id": sid})

    # 歌单
    def playlist_detail(self, pid):
        return self._req("/playlist/detail", {"id": pid})

    def playlist_track_all(self, pid, limit=1000, offset=0):
        return self._req("/playlist/track/all", {"id": pid, "limit": limit, "offset": offset})

    def user_playlist(self, uid, limit=50, offset=0):
        return self._req("/user/playlist", {"uid": uid, "limit": limit, "offset": offset})

    # 专辑 / 歌手
    def album_detail(self, aid):
        return self._req("/album", {"id": aid})

    def artist_detail(self, aid):
        return self._req("/artist/detail", {"id": aid})

    def artist_songs(self, aid, limit=50, offset=0, order="hot"):
        return self._req("/artist/songs", {"id": aid, "limit": limit, "offset": offset, "order": order})

    # 推荐 / FM
    def recommend_songs(self):
        return self._req("/recommend/songs")

    def recommend_playlists(self):
        return self._req("/recommend/resource")

    def personal_fm(self):
        return self._req("/personal_fm")

    # 喜欢 / 排行
    def like_song(self, sid, like=True):
        return self._req("/like", {"id": sid, "like": str(like).lower()})

    def toplist(self):
        return self._req("/toplist")


# ─────────────────────────── 下载器 ─────────────────────────────────

class Downloader:
    def __init__(self, api: NeteaseAPI, settings: Settings):
        self.api = api
        self.settings = settings
        self.download_dir = Path(settings.get("download_dir"))
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def _s(self, k):
        return self.settings.get(k)

    def _resolve_meta(self, sid, name=None, artists=None, album=None):
        if name and artists is not None:
            return name, artists, album or ""
        d = self.api.song_detail([sid])
        if d.get("code") == 200 and d.get("songs"):
            return extract_meta(d["songs"][0])
        return name or str(sid), artists or [], album or ""

    def _artists_str(self, names):
        return fmt_artists(names, self._s("max_artists"), self._s("artist_separator"))

    def _build_path(self, title, artists, album, sid, ext):
        art = self._artists_str(artists)
        rel = build_filename(self._s("filename_template"), title, art, album, sid, ext)
        return self.download_dir / rel

    def _get_song_url_info(self, sid, quality):
        """获取歌曲下载信息，返回 (url, info_dict) 或 (None, None)"""
        d = self.api.song_url([sid], level=quality)
        if d.get("code") != 200 or not d.get("data"):
            return None, None
        info = d["data"][0]
        url = info.get("url")
        return url, info

    def download_song(self, sid, name=None, artists=None, album=None, quality=None) -> bool:
        """单曲下载，带独立进度条"""
        quality = quality or self._s("quality")
        title, art_list, alb = self._resolve_meta(sid, name, artists, album)
        display = self._artists_str(art_list) + " - " + title

        url, info = self._get_song_url_info(sid, quality)
        if not url:
            msg_fail(f"{display}  (无法获取链接，可能需要VIP)")
            return False

        ext = (info.get("type") or "mp3").lower()
        filepath = self._build_path(title, art_list, alb, sid, ext)
        fname = filepath.name
        q_str, is_fb = resolve_quality(quality, info)

        if filepath.exists():
            msg_skip(fname)
            return True

        filepath.parent.mkdir(parents=True, exist_ok=True)
        fsize = info.get("size", 0) or 0

        q_style = "q_fallback" if is_fb else "q_normal"
        q_tag = f"[{q_style}]{q_str}[/{q_style}]"

        with Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task(fname, total=fsize or None)

            ok = self._stream(url, filepath, fsize,
                              on_chunk=lambda dl, tot: progress.update(task, completed=dl, total=tot))

        if ok:
            console.print(f"  [ok]\\[完成][/ok] {fname}  \\[{q_tag}]")
        else:
            msg_fail(f"{fname}  (下载失败)")
            if filepath.exists():
                filepath.unlink()
        return ok

    def download_songs(self, songs: list[dict], quality=None) -> tuple[int, int, int]:
        """
        批量下载。
        使用 rich Progress 显示:
          - 总进度条 (已完成文件数/总数)
          - 当前文件进度条 (字节)
        每首歌的完成状态通过 progress.console.print() 输出到进度条上方。
        """
        quality = quality or self._s("quality")
        total = len(songs)
        if total == 0:
            return 0, 0, 0
        if total == 1:
            s = songs[0]
            t, a, al = extract_meta(s)
            ok = self.download_song(s["id"], t, a, al, quality)
            return (1, 0, 0) if ok else (0, 0, 1)

        success = skipped = failed = 0
        q_display = QUALITY_NAMES.get(quality, quality)

        console.print()
        msg_info(f"批量下载: {total} 首，请求音质: [q_normal]{q_display}[/q_normal]")
        console.print()

        with Progress(
            TextColumn("[bold]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TextColumn("[dim]{task.fields[extra]}[/dim]"),
            console=console,
        ) as progress:
            overall = progress.add_task(f"[info]总进度[/info]", total=total, extra="")

            for idx, song in enumerate(songs):
                title, art_list, album = extract_meta(song)
                sid = song.get("id")
                display_artist = self._artists_str(art_list)
                short = f"{display_artist} - {title}"

                progress.update(overall, extra=short)

                url, info = self._get_song_url_info(sid, quality)
                if not url:
                    failed += 1
                    progress.console.print(f"  [err]\\[失败][/err] {short}  [dim](无法获取链接)[/dim]")
                    progress.advance(overall)
                    continue

                ext = (info.get("type") or "mp3").lower()
                filepath = self._build_path(title, art_list, album, sid, ext)
                fname = filepath.name
                q_str, is_fb = resolve_quality(quality, info)
                q_style = "q_fallback" if is_fb else "q_normal"
                q_tag = f"[{q_style}]{q_str}[/{q_style}]"

                if filepath.exists():
                    skipped += 1
                    progress.console.print(f"  [dim]\\[跳过][/dim] {fname}")
                    progress.advance(overall)
                    continue

                filepath.parent.mkdir(parents=True, exist_ok=True)
                fsize = info.get("size", 0) or 0

                # 添加当前文件的下载子任务
                file_task = progress.add_task(
                    f"  {fname}", total=fsize or None, extra=q_str,
                )

                ok = self._stream(
                    url, filepath, fsize,
                    on_chunk=lambda dl, tot, ft=file_task: progress.update(ft, completed=dl, total=tot),
                )

                progress.remove_task(file_task)

                if ok:
                    success += 1
                    progress.console.print(f"  [ok]\\[完成][/ok] {fname}  \\[{q_tag}]")
                else:
                    failed += 1
                    if filepath.exists():
                        filepath.unlink()
                    progress.console.print(f"  [err]\\[失败][/err] {fname}  [dim](下载出错)[/dim]")

                progress.advance(overall)

        console.print()
        parts = []
        if success:
            parts.append(f"[ok]{success} 成功[/ok]")
        if skipped:
            parts.append(f"[dim]{skipped} 跳过[/dim]")
        if failed:
            parts.append(f"[err]{failed} 失败[/err]")
        console.print(f"  [info]\\[统计][/info] {' / '.join(parts)}  (共 {total} 首)")
        console.print()
        return success, skipped, failed

    def _stream(self, url: str, filepath: Path, expected: int, on_chunk=None) -> bool:
        """流式下载文件"""
        tmp = filepath.with_suffix(filepath.suffix + ".tmp")
        try:
            resp = requests.get(url, stream=True, timeout=120)
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0)) or expected
            downloaded = 0
            with open(tmp, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if on_chunk:
                            on_chunk(downloaded, total)
            tmp.rename(filepath)
            return True
        except Exception:
            if tmp.exists():
                tmp.unlink()
            return False


# ─────────────────────────── 主程序 ──────────────────────────────────

class MusicCLI:
    def __init__(self):
        self.settings = Settings()
        self.api = None
        self.dl = None

    def _s(self, k):
        return self.settings.get(k)

    def init(self):
        console.print()
        console.print("  [title]网易云音乐下载器[/title]")
        console.print("  [dim]基于 NeteaseCloudMusicApi Enhanced[/dim]")
        console.print("  [dim]" + "=" * 40 + "[/dim]")
        console.print()

        cur = self._s("api_base")
        server = console.input(f"  API 服务器地址 \\[{cur}]: ").strip()
        if server and server != cur:
            self.settings.set("api_base", server)

        self.api = NeteaseAPI(self._s("api_base"))
        self.dl = Downloader(self.api, self.settings)

        msg_info(f"正在连接 {self._s('api_base')} ...")
        st = self.api.login_status()
        if st.get("code") == -1:
            msg_warn("连接失败，请检查服务器地址。")
        else:
            msg_ok("连接成功")
            if self.api.logged_in and self.api.user_info:
                msg_ok(f"已恢复会话: {self.api.user_info.get('nickname', '')}")
        console.print()

    # ──────── 主菜单 ────────

    def main_menu(self):
        while True:
            user = self.api.user_info.get("nickname", "") if self.api.logged_in else ""
            login_tag = f"[ok]{user}[/ok]" if user else "[dim]未登录[/dim]"
            q = QUALITY_NAMES.get(self._s("quality"), self._s("quality"))

            console.print(f"  [title]主菜单[/title]  {login_tag}  音质: [q_normal]{q}[/q_normal]")
            console.print("[dim]  " + "-" * 40 + "[/dim]")
            console.print("   1. 搜索与下载        5. 每日推荐")
            console.print("   2. 排行榜            6. 私人FM")
            console.print("   3. 我的歌单          7. 登录管理")
            console.print("   4. 歌词查看          8. 设置")
            console.print("   0. 退出")
            console.print("[dim]  " + "-" * 40 + "[/dim]")

            c = console.input("  > ").strip()
            if c == "0":
                console.print()
                msg_info("再见!")
                console.print()
                break
            elif c == "1":
                self.menu_search()
            elif c == "2":
                self.menu_toplist()
            elif c == "3":
                self.menu_my_playlists()
            elif c == "4":
                self.menu_lyrics()
            elif c == "5":
                self.menu_recommend()
            elif c == "6":
                self.menu_fm()
            elif c == "7":
                self.menu_auth()
            elif c == "8":
                self.menu_settings()
            else:
                msg_warn("无效选择")

    # ──────── 搜索与下载 (核心整合) ────────

    def menu_search(self):
        """
        统一搜索入口。搜索单曲后可直接下载；搜索歌手/专辑/歌单后
        可展开查看曲目并下载——不再需要跳回主菜单去不同的子菜单。
        """
        console.print()
        kw = console.input("  搜索关键词: ").strip()
        if not kw:
            return

        # 显示搜索类型
        type_parts = "  ".join(f"{k}.{v[1]}" for k, v in SEARCH_TYPES.items())
        console.print(f"  类型: {type_parts}")
        st = console.input("  选择类型 [默认 1-单曲]: ").strip() or "1"
        stype_id, stype_name = SEARCH_TYPES.get(st, (1, "单曲"))

        msg_info(f'搜索 "{kw}" ({stype_name})...')
        r = self.api.search(kw, stype=stype_id, limit=20)
        if r.get("code") != 200:
            msg_error("搜索失败")
            return

        data = r.get("result", {})

        if stype_id == 1:
            songs = data.get("songs", [])
            if not songs:
                msg_warn("未找到相关歌曲")
                return
            self._show_songs(songs)
            self._song_actions(songs)

        elif stype_id == 10:
            albums = data.get("albums", [])
            if not albums:
                msg_warn("未找到相关专辑")
                return
            self._show_album_list(albums)
            self._album_pick(albums)

        elif stype_id == 100:
            artists = data.get("artists", [])
            if not artists:
                msg_warn("未找到相关歌手")
                return
            self._show_artist_list(artists)
            self._artist_pick(artists)

        elif stype_id == 1000:
            pls = data.get("playlists", [])
            if not pls:
                msg_warn("未找到相关歌单")
                return
            self._show_playlist_list(pls)
            self._playlist_pick(pls)

        else:
            console.print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])

        console.print()

    # ── 歌曲列表展示与操作 ──

    def _show_songs(self, songs):
        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("歌名", max_width=28)
        t.add_column("歌手", max_width=22)
        t.add_column("专辑", max_width=20, style="dim")
        t.add_column("时长", width=6, style="dim")
        t.add_column("ID", style="dim", width=12)
        sep = self._s("artist_separator")
        mx = self._s("max_artists")
        for i, s in enumerate(songs, 1):
            _, artists, album = extract_meta(s)
            t.add_row(
                str(i), s.get("name", "")[:28],
                fmt_artists(artists, mx, sep)[:22],
                album[:20], fmt_duration(s.get("dt", s.get("duration", 0))),
                str(s.get("id", "")),
            )
        console.print(t)

    def _song_actions(self, songs):
        """歌曲列表操作循环"""
        while True:
            console.print()
            console.print("[hint]  操作: d <序号>  d <起>-<止>  d all | i <序号> 详情 | l <序号> 歌词 | q 返回[/hint]")
            act = console.input("  > ").strip().lower()
            if not act or act == "q":
                break

            parts = act.split(maxsplit=1)
            cmd, arg = parts[0], parts[1] if len(parts) > 1 else ""

            if cmd == "d":
                q = self._ask_quality()
                if arg == "all":
                    target = songs
                elif "-" in arg:
                    try:
                        a, b = arg.split("-")
                        target = songs[int(a) - 1:int(b)]
                    except (ValueError, IndexError):
                        msg_error("无效范围")
                        continue
                else:
                    try:
                        target = [songs[int(arg) - 1]]
                    except (ValueError, IndexError):
                        msg_error("无效序号")
                        continue
                self.dl.download_songs(target, q)

            elif cmd == "i":
                try:
                    self._song_detail(songs[int(arg) - 1]["id"])
                except (ValueError, IndexError):
                    msg_error("无效序号")

            elif cmd == "l":
                try:
                    self._show_lyrics(songs[int(arg) - 1]["id"])
                except (ValueError, IndexError):
                    msg_error("无效序号")

    # ── 专辑列表 → 展开 ──

    def _show_album_list(self, albums):
        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("专辑", max_width=30)
        t.add_column("歌手", max_width=22)
        t.add_column("日期", width=12, style="dim")
        t.add_column("ID", style="dim", width=12)
        sep, mx = self._s("artist_separator"), self._s("max_artists")
        for i, a in enumerate(albums, 1):
            arts = [ar.get("name", "") for ar in a.get("artists", [])]
            pub = ""
            if a.get("publishTime"):
                pub = datetime.fromtimestamp(a["publishTime"] / 1000).strftime("%Y-%m-%d")
            t.add_row(str(i), a.get("name", "")[:30],
                      fmt_artists(arts, mx, sep)[:22], pub, str(a.get("id", "")))
        console.print(t)

    def _album_pick(self, albums):
        """选择一张专辑 → 展开曲目列表 → 进入歌曲操作"""
        console.print()
        c = console.input("  输入序号查看专辑 (回车返回): ").strip()
        if not c:
            return
        try:
            album = albums[int(c) - 1]
        except (ValueError, IndexError):
            msg_error("无效序号")
            return

        msg_info(f'获取专辑 "{album.get("name")}" ...')
        r = self.api.album_detail(album["id"])
        if r.get("code") != 200:
            msg_error("获取专辑失败")
            return

        alb = r.get("album", {})
        songs = r.get("songs", [])
        arts = [a.get("name", "") for a in alb.get("artists", [])]
        art_str = fmt_artists(arts, self._s("max_artists"), self._s("artist_separator"))

        console.print()
        console.print(f"  [title]专辑[/title]: {alb.get('name', '')}")
        console.print(f"  [title]歌手[/title]: {art_str}")
        console.print(f"  [title]曲目[/title]: {len(songs)} 首")
        pub = alb.get("publishTime")
        if pub:
            console.print(f"  [title]日期[/title]: {datetime.fromtimestamp(pub / 1000).strftime('%Y-%m-%d')}")
        console.print()

        if not songs:
            msg_warn("此专辑暂无曲目")
            return
        self._show_songs(songs)
        self._song_actions_with_folder(songs, sanitize_filename(
            f"{sanitize_filename(art_str)} - {sanitize_filename(alb.get('name', ''))}"
        ))

    # ── 歌手列表 → 展开 ──

    def _show_artist_list(self, artists):
        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("歌手", max_width=25)
        t.add_column("歌曲数", width=8, style="dim")
        t.add_column("专辑数", width=8, style="dim")
        t.add_column("ID", style="dim", width=12)
        for i, a in enumerate(artists, 1):
            t.add_row(str(i), a.get("name", ""), str(a.get("musicSize", 0)),
                      str(a.get("albumSize", 0)), str(a.get("id", "")))
        console.print(t)

    def _artist_pick(self, artists):
        console.print()
        c = console.input("  输入序号查看歌手 (回车返回): ").strip()
        if not c:
            return
        try:
            artist = artists[int(c) - 1]
        except (ValueError, IndexError):
            msg_error("无效序号")
            return

        aid = artist["id"]
        aname = artist.get("name", str(aid))

        console.print()
        console.print(f"  [title]歌手[/title]: {aname}")
        console.print("  排序: 1.热度  2.时间")
        order = "time" if console.input("  [默认 1]: ").strip() == "2" else "hot"
        lim = console.input("  数量 [默认 50, 最大 100]: ").strip() or "50"
        try:
            lim = min(int(lim), 100)
        except ValueError:
            lim = 50

        msg_info(f"获取 {aname} 的歌曲 ({order}, {lim}首)...")
        r = self.api.artist_songs(aid, limit=lim, order=order)
        songs = r.get("songs", [])
        if not songs:
            msg_warn("未找到歌曲")
            return
        self._show_songs(songs)
        self._song_actions(songs)

    # ── 歌单列表 → 展开 ──

    def _show_playlist_list(self, pls):
        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("歌单", max_width=30)
        t.add_column("创建者", max_width=16, style="dim")
        t.add_column("曲目", width=6)
        t.add_column("播放", width=10, style="dim")
        t.add_column("ID", style="dim", width=12)
        for i, p in enumerate(pls, 1):
            t.add_row(str(i), p.get("name", "")[:30],
                      p.get("creator", {}).get("nickname", "")[:16],
                      str(p.get("trackCount", 0)),
                      str(p.get("playCount", 0)),
                      str(p.get("id", "")))
        console.print(t)

    def _playlist_pick(self, pls):
        console.print()
        c = console.input("  输入序号查看歌单 (回车返回): ").strip()
        if not c:
            return
        try:
            pl = pls[int(c) - 1]
        except (ValueError, IndexError):
            msg_error("无效序号")
            return
        self._open_playlist(pl["id"], pl.get("name", ""))

    def _open_playlist(self, pid, name=""):
        """打开一个歌单：获取信息 → 展示曲目 → 进入操作"""
        msg_info(f'获取歌单 "{name or pid}" ...')
        d = self.api.playlist_detail(pid)
        if d.get("code") != 200:
            msg_error("获取歌单失败")
            return

        pl = d.get("playlist", {})
        name = name or pl.get("name", "")
        console.print()
        console.print(f"  [title]歌单[/title]  : {pl.get('name', '')}")
        console.print(f"  [title]创建者[/title]: {pl.get('creator', {}).get('nickname', '')}")
        console.print(f"  [title]曲目[/title]  : {pl.get('trackCount', 0)} 首")
        console.print()

        msg_info("获取全部曲目...")
        td = self.api.playlist_track_all(pid)
        songs = td.get("songs", []) or pl.get("tracks", [])
        if not songs:
            msg_error("无法获取曲目列表")
            return

        msg_info(f"共 {len(songs)} 首曲目")
        self._show_songs(songs[:50])
        if len(songs) > 50:
            console.print(f"  [dim]... 还有 {len(songs) - 50} 首 ...[/dim]")

        self._song_actions_with_folder(songs, sanitize_filename(name))

    def _song_actions_with_folder(self, songs, folder_name: str):
        """
        与 _song_actions 类似，但批量下载时自动创建以 folder_name 命名的子文件夹。
        """
        while True:
            console.print()
            console.print("[hint]  操作: d <序号>  d <起>-<止>  d all | i <序号> | l <序号> | q 返回[/hint]")
            act = console.input("  > ").strip().lower()
            if not act or act == "q":
                break

            parts = act.split(maxsplit=1)
            cmd, arg = parts[0], parts[1] if len(parts) > 1 else ""

            if cmd == "d":
                q = self._ask_quality()
                if arg == "all":
                    target = songs
                elif "-" in arg:
                    try:
                        a, b = arg.split("-")
                        target = songs[int(a) - 1:int(b)]
                    except (ValueError, IndexError):
                        msg_error("无效范围")
                        continue
                else:
                    try:
                        target = [songs[int(arg) - 1]]
                    except (ValueError, IndexError):
                        msg_error("无效序号")
                        continue

                # 使用子文件夹
                saved = self.dl.download_dir
                self.dl.download_dir = Path(self._s("download_dir")) / truncate_bytes(folder_name, MAX_FILENAME_BYTES)
                self.dl.download_dir.mkdir(parents=True, exist_ok=True)
                self.dl.download_songs(target, q)
                self.dl.download_dir = saved

            elif cmd == "i":
                try:
                    self._song_detail(songs[int(arg) - 1]["id"])
                except (ValueError, IndexError):
                    msg_error("无效序号")
            elif cmd == "l":
                try:
                    self._show_lyrics(songs[int(arg) - 1]["id"])
                except (ValueError, IndexError):
                    msg_error("无效序号")

    def _ask_quality(self) -> str:
        cur = self._s("quality")
        console.print()
        for k, (code, name) in QUALITY_LEVELS.items():
            mark = " [ok]<[/ok]" if code == cur else ""
            console.print(f"    {k}. {name}{mark}")
        console.print(f"    0. 使用当前 ({QUALITY_NAMES.get(cur, cur)})")
        c = console.input("  音质> ").strip()
        if c in QUALITY_LEVELS:
            return QUALITY_LEVELS[c][0]
        return cur

    # ──────── 歌曲详情 ────────

    def _song_detail(self, sid):
        d = self.api.song_detail([sid])
        if d.get("code") != 200 or not d.get("songs"):
            msg_error("获取详情失败")
            return
        s = d["songs"][0]
        ar = s.get("ar", [])
        sep = self._s("artist_separator")
        art_full = sep.join(a["name"] for a in ar)
        album = s.get("al", {})
        dur = fmt_duration(s.get("dt", 0))

        fee_map = {0: "免费", 1: "VIP", 4: "购买专辑", 8: "免费/低音质"}

        console.print()
        console.print("[dim]  " + "-" * 38 + "[/dim]")
        console.print(f"  [title]歌名[/title]  : {s.get('name', '')}")
        console.print(f"  [title]歌手[/title]  : {art_full}")
        console.print(f"  [title]专辑[/title]  : {album.get('name', '')}")
        console.print(f"  [title]时长[/title]  : {dur}")
        console.print(f"  [title]ID[/title]    : {s.get('id')}")
        console.print(f"  [title]收费[/title]  : {fee_map.get(s.get('fee', 0), str(s.get('fee', '')))}")
        sq = s.get("sq")
        hr = s.get("hr")
        if sq:
            console.print(f"  [title]无损[/title]  : 可用 ({sq.get('br', 0) // 1000}kbps)")
        if hr:
            console.print(f"  [title]Hi-Res[/title]: 可用 ({hr.get('br', 0) // 1000}kbps)")
        pub = s.get("publishTime")
        if pub:
            console.print(f"  [title]发布[/title]  : {datetime.fromtimestamp(pub / 1000).strftime('%Y-%m-%d')}")
        console.print("[dim]  " + "-" * 38 + "[/dim]")

    # ──────── 歌词 ────────

    def _show_lyrics(self, sid):
        r = self.api.lyric(sid)
        if r.get("code") != 200:
            msg_error("获取歌词失败")
            return
        lrc = r.get("lrc", {}).get("lyric", "")
        tlrc = r.get("tlyric", {}).get("lyric", "")
        if not lrc:
            msg_warn("暂无歌词")
            return

        console.print()
        console.print("[dim]  " + "-" * 38 + "[/dim]")
        console.print("  [title]歌词[/title]")
        console.print("[dim]  " + "-" * 38 + "[/dim]")
        for line in lrc.split("\n"):
            c = re.sub(r'\[[\d:.]+\]', '', line).strip()
            if c:
                console.print(f"  {c}")
        if tlrc:
            console.print()
            console.print("  [title]翻译[/title]")
            console.print("[dim]  " + "-" * 38 + "[/dim]")
            for line in tlrc.split("\n"):
                c = re.sub(r'\[[\d:.]+\]', '', line).strip()
                if c:
                    console.print(f"  [dim]{c}[/dim]")
        console.print("[dim]  " + "-" * 38 + "[/dim]")

    def menu_lyrics(self):
        console.print()
        sid = console.input("  输入歌曲 ID: ").strip()
        if sid:
            try:
                self._show_lyrics(int(sid))
            except ValueError:
                msg_error("无效 ID")
        console.print()

    # ──────── 排行榜 ────────

    def menu_toplist(self):
        console.print()
        r = self.api.toplist()
        if r.get("code") != 200:
            msg_error("获取排行榜失败")
            return
        lists = r.get("list", [])
        if not lists:
            msg_warn("暂无数据")
            return

        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("排行榜", max_width=28)
        t.add_column("更新", width=12, style="dim")
        t.add_column("曲目", width=6)
        for i, item in enumerate(lists, 1):
            t.add_row(str(i), item.get("name", "")[:28],
                      item.get("updateFrequency", ""), str(item.get("trackCount", 0)))
        console.print(t)

        c = console.input("\n  输入序号查看 (回车返回): ").strip()
        if c:
            try:
                pl = lists[int(c) - 1]
                self._open_playlist(pl["id"], pl.get("name", ""))
            except (ValueError, IndexError):
                msg_error("无效序号")
        console.print()

    # ──────── 每日推荐 ────────

    def menu_recommend(self):
        console.print()
        if not self.api.logged_in:
            msg_error("请先登录")
            return
        console.print("  1. 每日推荐歌曲")
        console.print("  2. 推荐歌单")
        console.print("  0. 返回")
        c = console.input("  > ").strip()
        if c == "1":
            r = self.api.recommend_songs()
            if r.get("code") != 200:
                msg_error("获取推荐失败")
                return
            songs = r.get("data", {}).get("dailySongs", [])
            if not songs:
                msg_warn("暂无推荐")
                return
            msg_info(f"每日推荐: {len(songs)} 首")
            self._show_songs(songs)
            self._song_actions(songs)
        elif c == "2":
            r = self.api.recommend_playlists()
            if r.get("code") != 200:
                msg_error("获取推荐失败")
                return
            pls = r.get("recommend", [])
            if not pls:
                msg_warn("暂无推荐")
                return
            self._show_playlist_list(pls)
            self._playlist_pick(pls)
        console.print()

    # ──────── 私人FM ────────

    def menu_fm(self):
        console.print()
        if not self.api.logged_in:
            msg_error("请先登录")
            return
        console.print("  [title]私人FM[/title]")
        while True:
            r = self.api.personal_fm()
            if r.get("code") != 200:
                msg_error("获取FM失败")
                break
            songs = r.get("data", [])
            if not songs:
                msg_warn("暂无推荐")
                break

            s = songs[0]
            arts = [a.get("name", "") for a in s.get("artists", [])]
            dur = fmt_duration(s.get("duration", 0))
            console.print()
            console.print(f"    [title]{s.get('name', '')}[/title]")
            console.print(f"    {fmt_artists(arts, self._s('max_artists'), self._s('artist_separator'))}")
            console.print(f"    [dim]{s.get('album', {}).get('name', '')} | {dur} | ID: {s.get('id')}[/dim]")
            console.print()
            console.print("[hint]    d=下载  n=下一首  l=喜欢  q=返回[/hint]")

            a = console.input("    > ").strip().lower()
            if a == "q":
                break
            elif a == "d":
                q = self._ask_quality()
                self.dl.download_song(s["id"], s.get("name"), arts,
                                      s.get("album", {}).get("name", ""), q)
            elif a == "l":
                lr = self.api.like_song(s["id"], True)
                if lr.get("code") == 200:
                    msg_ok("已喜欢!")
                else:
                    msg_error("操作失败")
        console.print()

    # ──────── 我的歌单 ────────

    def menu_my_playlists(self):
        console.print()
        if not self.api.logged_in:
            msg_error("请先登录")
            return
        uid = self.api.user_info.get("userId")
        if not uid:
            msg_error("无法获取用户 ID")
            return
        r = self.api.user_playlist(uid, limit=50)
        if r.get("code") != 200:
            msg_error("获取歌单失败")
            return
        pls = r.get("playlist", [])
        if not pls:
            msg_warn("暂无歌单")
            return

        t = Table(show_lines=False, padding=(0, 1))
        t.add_column("#", style="dim", width=4)
        t.add_column("歌单", max_width=30)
        t.add_column("曲目", width=6)
        t.add_column("播放", width=10, style="dim")
        t.add_column("ID", style="dim", width=12)
        for i, p in enumerate(pls, 1):
            name = p.get("name", "")
            if p.get("creator", {}).get("userId") == uid:
                name = "[*] " + name
            t.add_row(str(i), name[:30], str(p.get("trackCount", 0)),
                      str(p.get("playCount", 0)), str(p.get("id", "")))
        console.print(t)

        c = console.input("\n  输入序号查看 (回车返回): ").strip()
        if c:
            try:
                pl = pls[int(c) - 1]
                self._open_playlist(pl["id"], pl.get("name", ""))
            except (ValueError, IndexError):
                msg_error("无效序号")
        console.print()

    # ──────── 登录管理 ────────

    def menu_auth(self):
        console.print()
        if self.api.logged_in:
            msg_info(f"当前用户: [title]{self.api.user_info.get('nickname', '')}[/title]")
            console.print("  1. 刷新会话")
            console.print("  2. 退出登录")
            console.print("  0. 返回")
            c = console.input("  > ").strip()
            if c == "1":
                self.api.login_refresh()
                st = self.api.login_status()
                p = st.get("data", {}).get("profile")
                if p:
                    self.api.user_info = p
                    self.api.logged_in = True
                    self.api._save_cookie()
                    msg_ok(f"会话有效: {p.get('nickname')}")
                else:
                    msg_warn("会话已过期，请重新登录")
                    self.api.logged_in = False
                    self.api._save_cookie()
            elif c == "2":
                self.api.logout()
                msg_ok("已退出登录")
            console.print()
            return

        console.print("  登录方式:")
        console.print("  1. 手机号")
        console.print("  2. 邮箱")
        console.print("  3. 二维码")
        console.print("  0. 返回")
        c = console.input("  > ").strip()
        if c == "1":
            self._login_phone()
        elif c == "2":
            self._login_email()
        elif c == "3":
            self._login_qr()
        console.print()

    def _login_phone(self):
        cc = console.input("  国家码 [86]: ").strip() or "86"
        phone = console.input("  手机号: ").strip()
        pw = console.input("  密码: ").strip()
        if not phone or not pw:
            msg_error("手机号和密码不能为空")
            return
        msg_info("登录中...")
        r = self.api.login_phone(phone, pw, cc)
        if r.get("code") == 200:
            msg_ok(f"欢迎, {self.api.user_info.get('nickname', '')}!")
        else:
            msg_error(f"登录失败: {r.get('message', r.get('msg', '未知错误'))}")

    def _login_email(self):
        email = console.input("  邮箱: ").strip()
        pw = console.input("  密码: ").strip()
        if not email or not pw:
            msg_error("邮箱和密码不能为空")
            return
        msg_info("登录中...")
        r = self.api.login_email(email, pw)
        if r.get("code") == 200:
            msg_ok(f"欢迎, {self.api.user_info.get('nickname', '')}!")
        else:
            msg_error(f"登录失败: {r.get('message', r.get('msg', '未知错误'))}")

    def _login_qr(self):
        kd = self.api.login_qr_key()
        unikey = kd.get("data", {}).get("unikey")
        if not unikey:
            msg_error("获取二维码失败")
            return
        qd = self.api.login_qr_create(unikey)
        qr_url = qd.get("data", {}).get("qrurl", "")
        if qr_url:
            try:
                import qrcode
                qr = qrcode.QRCode(version=1, box_size=1, border=1)
                qr.add_data(qr_url)
                qr.make(fit=True)
                console.print()
                msg_info("请使用网易云音乐 APP 扫描:")
                qr.print_ascii(invert=True)
            except ImportError:
                msg_info(f"扫码链接: {qr_url}")
                msg_info("(安装 qrcode 可在终端显示: pip install qrcode)")
        else:
            msg_error("生成二维码失败")
            return

        msg_info("等待扫码...")
        while True:
            time.sleep(3)
            ch = self.api.login_qr_check(unikey)
            code = ch.get("code")
            if code == 800:
                msg_error("二维码已过期")
                return
            elif code == 802:
                msg_info("已扫码，请在手机上确认...")
            elif code == 803:
                self.api.cookie_str = ch.get("cookie", "")
                self.api.logged_in = True
                st = self.api.login_status()
                p = st.get("data", {}).get("profile", {})
                if p:
                    self.api.user_info = p
                msg_ok(f"登录成功! 欢迎, {p.get('nickname', '')}!")
                self.api._save_cookie()
                return

    # ──────── 设置 ────────

    def menu_settings(self):
        while True:
            q = QUALITY_NAMES.get(self._s("quality"), self._s("quality"))
            tmpl = self._s("filename_template")
            tmpl_desc = next((v[1] for v in FILENAME_TEMPLATES.values() if v[0] == tmpl), tmpl)
            sep = self._s("artist_separator")

            console.print()
            console.print("  [title]设置[/title]  [dim](修改后自动保存)[/dim]")
            console.print("[dim]  " + "-" * 45 + "[/dim]")
            console.print(f"  1. 默认音质      : [q_normal]{q}[/q_normal]")
            console.print(f"  2. 下载目录      : {self._s('download_dir')}")
            console.print(f"  3. API 服务器    : {self._s('api_base')}")
            console.print(f"  4. 文件命名格式  : {tmpl_desc}")
            console.print(f"  5. 歌手显示上限  : {self._s('max_artists')}")
            console.print(f"  6. 歌手分隔符    : {repr(sep)}")
            console.print("  0. 返回")

            c = console.input("  > ").strip()
            if c == "0":
                break
            elif c == "1":
                nq = self._ask_quality()
                self.settings.set("quality", nq)
                msg_ok(f"默认音质: {QUALITY_NAMES.get(nq, nq)}")
            elif c == "2":
                nd = console.input(f"  新目录 [{self._s('download_dir')}]: ").strip()
                if nd:
                    self.settings.set("download_dir", nd)
                    self.dl.download_dir = Path(nd)
                    self.dl.download_dir.mkdir(parents=True, exist_ok=True)
                    msg_ok(f"下载目录: {nd}")
            elif c == "3":
                nu = console.input(f"  新地址 [{self._s('api_base')}]: ").strip()
                if nu:
                    self.settings.set("api_base", nu)
                    self.api.base_url = nu.rstrip("/")
                    msg_ok(f"API 服务器: {nu}")
            elif c == "4":
                self._set_template()
            elif c == "5":
                ns = console.input(f"  上限 [{self._s('max_artists')}]: ").strip()
                try:
                    n = int(ns)
                    if n < 1:
                        raise ValueError
                    self.settings.set("max_artists", n)
                    demo = ["歌手A", "歌手B", "歌手C", "歌手D", "歌手E"]
                    msg_ok(f"歌手上限: {n}")
                    msg_info(f"示例: {fmt_artists(demo, n, self._s('artist_separator'))}")
                except ValueError:
                    msg_error("请输入正整数")
            elif c == "6":
                self._set_separator()
        console.print()

    def _set_template(self):
        console.print()
        console.print("  可用变量: {title} {artist} {album} {id}")
        console.print("  使用 / 可自动创建子文件夹")
        console.print()
        for k, (tmpl, desc) in FILENAME_TEMPLATES.items():
            mark = " [ok]<[/ok]" if tmpl == self._s("filename_template") else ""
            console.print(f"    {k}. {desc}  ->  {tmpl}{mark}")
        console.print("    c. 自定义输入")
        console.print("    0. 取消")
        c = console.input("  > ").strip()
        if c == "0":
            return
        elif c == "c":
            custom = console.input("  模板 (如 {artist} - {title}): ").strip()
            if not custom:
                return
            if "{title}" not in custom and "{id}" not in custom:
                msg_warn("模板中至少需要 {title} 或 {id}")
                return
            self.settings.set("filename_template", custom)
        elif c in FILENAME_TEMPLATES:
            self.settings.set("filename_template", FILENAME_TEMPLATES[c][0])
        else:
            msg_error("无效选择")
            return
        preview = build_filename(
            self._s("filename_template"), "晴天",
            fmt_artists(["周杰伦"], self._s("max_artists"), self._s("artist_separator")),
            "叶惠美", 186016, "mp3",
        )
        msg_ok(f"命名格式: {self._s('filename_template')}")
        msg_info(f"预览: {preview}")

    def _set_separator(self):
        console.print()
        presets = {
            "1": (" / ", '" / "  (A / B / C)'),
            "2": (", ", '", "   (A, B, C)'),
            "3": (" & ", '" & "  (A & B & C)'),
            "4": ("\u3001", '"\u3001"   (A\u3001B\u3001C)'),
            "5": (" x ", '" x "  (A x B x C)'),
        }
        for k, (_, desc) in presets.items():
            mark = " [ok]<[/ok]" if presets[k][0] == self._s("artist_separator") else ""
            console.print(f"    {k}. {desc}{mark}")
        console.print("    c. 自定义")
        console.print("    0. 取消")
        c = console.input("  > ").strip()
        if c == "0":
            return
        elif c == "c":
            custom = console.input("  分隔符: ")
            if custom:
                self.settings.set("artist_separator", custom)
            else:
                msg_error("不能为空")
                return
        elif c in presets:
            self.settings.set("artist_separator", presets[c][0])
        else:
            msg_error("无效选择")
            return
        demo = ["周杰伦", "林俊杰", "陈奕迅", "李荣浩", "薛之谦"]
        msg_ok(f"分隔符: {repr(self._s('artist_separator'))}")
        msg_info(f"示例: {fmt_artists(demo, self._s('max_artists'), self._s('artist_separator'))}")


# ─────────────────────────── 入口 ────────────────────────────────────

def main():
    cli = MusicCLI()
    cli.init()
    cli.main_menu()


if __name__ == "__main__":
    main()
