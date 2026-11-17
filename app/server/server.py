#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""媒体画质管家 - fnOS app backend (stdlib only)."""
import argparse
import concurrent.futures
import csv
import hashlib
import io
import json
import os
import re
import shutil
import socket
import socketserver
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---- 统一网关相关 ----
PREFIX = ""                     # 如 /app/mediaqc，由网关传入
REQUIRE_AUTH = True             # 要求请求带网关注入的用户身份头
GATEWAY_USER_HEADERS = ("X-Trim-Userid", "Trim-Userid", "X-Trim-Username")
AUTH_LOG_DONE = False
RAW_LOG_COUNT = 0

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WWW_DIR = os.path.join(APP_DIR, "www")

VOL_ROOT_RE = re.compile(r"^/vol\d+$")
# 卷根目录下的系统目录（浏览时隐藏，避免误选）
SYS_DIR_NAMES = {
    "appcenter-downloads", "docker", "mediasrv.transcode", "thumb", "vm",
    "lost+found", "#recycle", "recycle", "snapshots", "homes", "appstore",
    "@eaDir", ".@__thumb", "tmp",
}

VIDEO_EXT = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mts", ".wmv", ".mov", ".m4v",
    ".flv", ".rmvb", ".rm", ".mpg", ".mpeg", ".webm", ".vob", ".f4v", ".asf",
}
OLD_CODECS = {"wmv3", "wmv2", "wmv1", "msmpeg4v3", "msmpeg4v2", "mpeg2video",
              "vc1", "rv40", "rv30", "flv1", "h263", "mpeg4"}

# 三层评分基准（按用户 19 个样本人工打分回归校准：RMSE 28.3 → 16.6）
RES_BASE = {2160: 100, 1440: 88, 1080: 75, 720: 60,
            480: 20, 360: 14, 240: 9, 0: 5}
BR_TIERS = {2160: (25.0, 14.0, 7.0), 1440: (18.0, 10.0, 5.0), 1080: (8.0, 7.0, 3.4),
            720: (3.5, 2.2, 1.0), 480: (2.0, 1.2, 0.6), 360: (1.6, 1.0, 0.5),
            240: (1.2, 0.8, 0.4), 0: (1.0, 0.6, 0.3)}
TIER_LABEL = {2160: "4K", 1440: "1440p", 1080: "1080p", 720: "720p",
              480: "480p", 360: "360p", 240: "240p", 0: "<240p"}


def res_tier(w, h):
    """档位判定同时看长边与短边：
       - 1920x820（2.35:1 无黑边宽银幕）→ 1080p（只看短边会误判成 720p）
       - 1436x1080（4:3 裁切）→ 1080p（只看长边会误判成 720p）
       - 1080x1920（竖屏）→ 1080p"""
    w, h = int(w or 0), int(h or 0)
    long_side, short = max(w, h), min(w, h)
    if long_side >= 3400 or short >= 2000:
        return 2160
    if long_side >= 2400:
        return 1440
    if long_side >= 1800 or short >= 1000:
        return 1080
    if long_side >= 1200 or short >= 700:
        return 720
    # SD 细分：320x240 与 704x352 不该同分
    if short >= 480:
        return 480
    if short >= 360:
        return 360
    if short >= 240:
        return 240
    return 0


def res_score_of(tier):
    return RES_BASE.get(tier, 45)


def res_score_cont(w, h):
    """分辨率连续分：按实际像素数（1080p=75，4K≈100，720p≈52，480p≈35）"""
    px = max(1.0, float(w or 0) * float(h or 0))
    r = math.sqrt(px / (1920.0 * 1080.0))
    s = 75.0 * r if r <= 1.0 else 75.0 + 25.0 * (r - 1.0)
    return max(0.0, min(100.0, s))


# bpp（每像素比特，帧率按 30 折算）→ 0~100
BPP_PTS = [(0.020, 0.0), (0.035, 15.0), (0.060, 40.0), (0.090, 65.0),
           (0.130, 85.0), (0.200, 100.0)]


def bpp_score_of(bpp):
    x = float(bpp or 0)
    if x <= BPP_PTS[0][0]:
        return BPP_PTS[0][1]
    for i in range(1, len(BPP_PTS)):
        x0, y0 = BPP_PTS[i - 1]
        x1, y1 = BPP_PTS[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-6, (x1 - x0))
    return 100.0


def br_score_of(vbitrate, tier):
    """码率分：按分辨率档位归一后线性内插（0-100）。"""
    mbps = max(0.0, (vbitrate or 0) / 1e6)
    hi, mid, low = BR_TIERS.get(tier, BR_TIERS[0])
    if mbps >= hi:
        return 100.0
    if mbps >= mid:
        return 85.0 + 15.0 * (mbps - mid) / max(1e-6, (hi - mid))
    if mbps >= low:
        return 50.0 + 35.0 * (mbps - low) / max(1e-6, (mid - low))
    return max(0.0, 50.0 * mbps / max(1e-6, low))
LOWQ_WORDS = ["cam", "枪版", "抢先", "hdtv", "scr", "ts版", "tc版", "hdts", "清晰版", "国语无字"]

FFPROBE = shutil.which("ffprobe") or "/usr/bin/ffprobe"
FFMPEG = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"

LOCK = threading.Lock()
JOB = {"id": None, "state": "idle", "phase": "", "total": 0, "done": 0, "found": 0,
       "new": 0, "updated": 0, "cur": "", "msg": "", "t0": 0, "ended": 0}
JOB_STOP = threading.Event()

DEFAULT_CFG = {
    "cfg_version": 36,         # 配置口径版本：升级时自动迁移旧配置
    "port": 8123,
    "pin": "",
    "roots": [],
    "concurrency": 4,
    # 参数评分阈值
    "min_width": 1280,
    "bpp_min": 0.04,
    "br_1080": 2000000,
    "br_720": 1000000,
    # 画面体检
    "inspect_on_scan": True,   # 扫描时同时体检
    "q_frames": 16,            # 抽帧数量（16 帧取 p20，抵消场景差异；只解关键帧）
    "block_penalty_on": 0,     # 块效应是否扣分（0=只出标签，1=按曲线扣分）
    "bpp_adjust_on": 1,        # bpp 调整：欠码率扣分 / 高码率密度加分（带分辨率与清晰度护栏）
    "q_concurrency": 4,        # 体检并发（1~6，机械盘建议 ≤4）
    "use_gpu": 0,              # 1=用 NVIDIA 硬解（实测提升有限，瓶颈在磁盘）
    # 大文件降帧已取消：所有文件统一用 q_frames
    # 画面指标阈值（ffmpeg blurdetect，原生像素网格 1280x720 中心裁剪）
    # 本机库实测 blur: p10=6.5 p25=7.7 p50=9.5 p75=12.0 p90=15.3 max=30.1
    "q_blur_floor": 4.0,    # 边缘宽度 ≤ 此值 → 画面分满分
    "q_blur_soft": 6.0,     # → 80 分
    "q_blur_mid": 8.0,      # → 58 分（偏软/疑似拉伸）
    "q_blur_bad": 11.0,     # → 32 分（明显模糊）
    "q_block_mid": 1.8,     # 有压缩块效应
    "q_block_bad": 3.0,     # 块效应明显（本机 p90=3.3）
    "q_dark": 40,           # 画面过暗（Y 均值低于此值）
    "q_flat": 45,           # 对比度偏低（YHIGH-YLOW 低于此值）
    "q_flat_yavg": 70,      # 仅当画面整体偏暗时才判"对比度低"，避免夜景误伤
    # 综合分与结论（"能接受就留"：只对明显缺陷降档）
    "w_param": 0.3,
    "w_quality": 0.7,
    "param_floor": 60,      # 参数分低于此值 → 综合分不得超过参数分
    "q_floor": 30,          # 画面分低于此值 → 综合分最高 q_floor_cap
    "q_floor_cap": 45,
    "verdict_clean": 45,
    "verdict_good": 92,     # 优质：综合分 ≥
    "good_quality": 85,     # 优质：画面分还必须 ≥
    "suspect_param": 90,
    "suspect_quality": 70,
    # ---- 三层评分（分辨率 ≈ 码率 > 清晰度；按人工打分校准）----
    "caps_enabled": 0,      # 0=关闭所有封顶（只看加权分）；1=启用
    "w_res": 0.25,
    "w_bpp": 0.20,          # 每像素比特（压缩强度）
    "w_clarity": 0.40,      # 感知清晰度
    "w_block": 0.15,        # 块效应
    "w_br": 0.0,            # 码率不再参与加权（码率高 ≠ 更清晰）
    "br_penalty_from": 60,  # 码率分低于此值视为欠码率
    "br_penalty_k": 0.0,     # 只按清晰度算时，欠码率不再扣分
    "cap_block_score": 30,  # 块效应分低于此值 → 封顶
    "cap_block": 50,
    "cap_contrast": 58,     # 对比度偏低（发灰）→ 封顶
    # 封顶值必须都低于 verdict_bad，保证"结论"与"分数排序"严格一致（不会出现高分判差）
    "cap_clarity": 55,      # 清晰度很差的综合分封顶
    "cap_br": 50,           # 严重欠码率的综合分封顶
    "cap_oldcodec": 55,     # 老编码@高分辨率的综合分封顶
    # bpp（每像素比特）封顶：压缩强度判据
    "cap_bpp_1080_lo": 0.070,     # 1080p bpp 低于此值 = 压缩过重
    "cap_bpp_1080_lo_val": 55,
    "cap_bpp_1080_hi": 0.120,     # 1080p bpp 低于此值 = 压缩偏重（优质 WEB-DL 一般 ≥0.12）
    "cap_bpp_1080_hi_val": 65,
    "cap_bpp_lo_clarity": 60,     # 压缩过重时，清晰度还低于此值才封顶
    "cap_bpp_soft_clarity": 50,   # 压缩偏重时，清晰度还低于此值 → 封 55
    "cap_bpp_hi_clarity": 70,     # 压缩偏重时，清晰度低于此值 → 封 65
    "cap_bpp_1080_soft_val": 55,
    "cap_bpp_4k": 0.090,          # 4K/1440p bpp 低于此值
    "cap_bpp_4k_val": 65,
    "cap_480": 32,          # 480p 封顶
    "cap_360": 22,          # 360p 封顶
    "cap_240": 14,          # 240p 封顶
    "cap_low": 10,          # <240p 封顶
    "cap_sd": 35,           # 兼容旧键（已由上面细分档取代）
    "cap_720": 55,          # 720p 且码率不足时的综合分封顶
    "cap_720_br": 55,       # 仅当码率分低于此值才触发上面的封顶
    "verdict_bad": 60,      # 结论：综合评分 < 此值 = 差，否则 = 优秀
    "good_total2": 80,      # 优质：综合分 ≥
    "good_br": 80,          # 优质：码率分还必须 ≥
    "verdict_clean2": 45,   # 建议清理：综合分 <
    "verdict_replace2": 60,  # 建议换源：综合分 <
    # 目录浏览：隐藏系统目录
    "hide_sys_dirs": True,
    # 安全：要求请求携带飞牛网关注入的身份头（异常时可临时关闭）
    "require_gateway_auth": True,
}


def now():
    return time.time()


class Store:
    def __init__(self, data_dir, conf_dir):
        self.data_dir = data_dir
        self.conf_dir = conf_dir
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, "thumbs"), exist_ok=True)
        os.makedirs(conf_dir, exist_ok=True)
        self.cfg_path = os.path.join(conf_dir, "config.json")
        self.db_path = os.path.join(data_dir, "mediaqc.db")
        self.cfg = self.load_cfg()
        self.init_db()

    def load_cfg(self):
        cfg = dict(DEFAULT_CFG)
        raw = {}
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or {}
        except Exception:
            raw = {}
        cfg.update(raw)
        # 旧键清理（v1 画面指标 + 已废弃的 cap_score）
        cfg.pop("cap_score", None)
        for legacy in ("q_noise_mid", "q_noise_high", "q_blk_mid", "q_blk_high",
                       "q_psnr_mid", "q_psnr_high", "q_sharp_mid", "q_sharp_low"):
            cfg.pop(legacy, None)
        # 版本迁移：必须看"文件里的版本"，不能用合并默认值后的值（否则永远不触发）
        try:
            ver = int(raw.get("cfg_version") or 1)
        except Exception:
            ver = 1
        if ver < 36:
            for k in ("w_res", "w_bpp", "w_clarity", "w_block", "w_br",
                      "br_penalty_from", "br_penalty_k", "caps_enabled",
                      "cap_block", "cap_block_score", "cap_contrast",
                      "cap_bpp_1080_lo", "cap_bpp_1080_lo_val", "cap_bpp_1080_hi",
                      "cap_bpp_1080_hi_val", "cap_bpp_soft_clarity", "cap_bpp_1080_soft_val",
                      "cap_bpp_4k", "cap_bpp_4k_val",
                      "cap_clarity", "cap_br", "cap_oldcodec",
                      "cap_480", "cap_360", "cap_240", "cap_low",
                      "cap_sd", "cap_720", "cap_720_br", "good_total2", "good_br", "verdict_bad",
                      "verdict_clean2", "verdict_replace2",
                      "q_blur_floor", "q_blur_soft", "q_blur_mid", "q_blur_bad",
                      "q_block_mid", "q_block_bad", "q_dark", "q_flat", "q_flat_yavg", "q_frames"):
                cfg[k] = DEFAULT_CFG[k]
            cfg["cfg_version"] = 36
            cfg["q_frames"] = 16
            cfg.pop("q_big_gb", None)      # 大文件降帧已取消，统一用 q_frames
            cfg.pop("q_big_frames", None)
            cfg["q_concurrency"] = 4
            cfg["block_penalty_on"] = 0     # 块效应默认只出标签、不扣分
            cfg["bpp_adjust_on"] = 1        # bpp 调整（欠码率扣分 / 码率密度加分）
            cfg.setdefault("use_gpu", 0)
            print("config migrated to cfg_version=36 (bpp adjust with guards)", flush=True)
        return cfg

    def save_cfg(self):
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.cfg_path)

    def db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self.db()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS files(
            id INTEGER PRIMARY KEY, path TEXT UNIQUE, name TEXT, dir TEXT,
            size INTEGER, mtime REAL, width INTEGER, height INTEGER, fps REAL,
            bitrate INTEGER, vcodec TEXT, pix_fmt TEXT, acodec TEXT, channels INTEGER,
            duration REAL, container TEXT, score INTEGER, flags TEXT, reason TEXT,
            grp TEXT, state TEXT DEFAULT 'keep', probed_at REAL);
        CREATE TABLE IF NOT EXISTS jobs(
            id INTEGER PRIMARY KEY, kind TEXT, state TEXT, total INTEGER, done INTEGER,
            cur TEXT, msg TEXT, started REAL, ended REAL);
        CREATE TABLE IF NOT EXISTS audit(
            id INTEGER PRIMARY KEY, ts REAL, action TEXT, path TEXT, bytes INTEGER,
            result TEXT, detail TEXT);
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE INDEX IF NOT EXISTS idx_files_grp ON files(grp);
        CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir);
        """)
        # --- 轻量迁移：老库自动补列 ---
        have = {r["name"] for r in conn.execute("PRAGMA table_info(files)").fetchall()}
        for col, decl in (("abitrate", "INTEGER DEFAULT 0"), ("vbitrate", "INTEGER DEFAULT 0"),
                          ("q_score", "INTEGER"), ("q_flags", "TEXT"), ("q_detail", "TEXT"),
                          ("q_checked", "REAL"), ("total_score", "INTEGER"), ("verdict", "TEXT"),
                          ("ctransfer", "TEXT"), ("cprimaries", "TEXT"),
                          ("res_score", "INTEGER"), ("br_score", "INTEGER"),
                          ("clarity_score", "INTEGER")):
            if col not in have:
                conn.execute("ALTER TABLE files ADD COLUMN %s %s" % (col, decl))
        conn.commit()
        conn.close()


STORE = None


def is_media(name):
    return os.path.splitext(name)[1].lower() in VIDEO_EXT


def under_roots(path, roots):
    try:
        rp = os.path.realpath(path)
    except Exception:
        return False
    for r in roots:
        try:
            rr = os.path.realpath(r)
        except Exception:
            continue
        if rp == rr or rp.startswith(rr + os.sep):
            return True
    return False


def probe_file(path):
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", "-i", path]
    out = subprocess.run(cmd, capture_output=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.decode("utf-8", "ignore")[:300])
    data = json.loads(out.stdout.decode("utf-8", "ignore") or "{}")
    fmt = data.get("format") or {}
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    auds = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not v:
        raise RuntimeError("no video stream")
    fps = 0.0
    for key in ("avg_frame_rate", "r_frame_rate"):
        val = v.get(key) or "0/0"
        try:
            num, den = val.split("/")
            if float(den) > 0:
                fps = float(num) / float(den)
                break
        except Exception:
            pass
    bitrate = 0
    try:
        bitrate = int(fmt.get("bit_rate") or 0)
    except Exception:
        bitrate = 0
    if not bitrate:
        try:
            bitrate = int(v.get("bit_rate") or 0)
        except Exception:
            bitrate = 0
    try:
        duration = float(fmt.get("duration") or v.get("duration") or 0)
    except Exception:
        duration = 0.0
    a0 = auds[0] if auds else {}
    # 音轨总码率（多条音轨相加），缺失时按声道估：128k/声道
    abitrate = 0
    for a in auds:
        try:
            abitrate += int(a.get("bit_rate") or 0)
        except Exception:
            pass
    if not abitrate and auds:
        abitrate = 128000 * max(1, int(a0.get("channels") or 2))
    # 视频真实码率 = 文件大小*8/时长 - 音轨码率（比容器/流字段更可信）
    vbitrate = 0
    try:
        fsize = os.path.getsize(path)
    except Exception:
        fsize = 0
    if fsize and duration > 0:
        vbitrate = int(max(0, fsize * 8 / duration - abitrate))
    elif v.get("bit_rate"):
        vbitrate = int(v.get("bit_rate") or 0)
    return {
        "width": int(v.get("width") or 0),
        "height": int(v.get("height") or 0),
        "fps": round(fps, 3),
        "bitrate": bitrate,
        "abitrate": abitrate,
        "vbitrate": vbitrate or bitrate,
        "vcodec": v.get("codec_name") or "",
        "pix_fmt": v.get("pix_fmt") or "",
        "ctransfer": (v.get("color_transfer") or ""),
        "cprimaries": (v.get("color_primaries") or ""),
        "acodec": a0.get("codec_name") or "",
        "channels": int(a0.get("channels") or 0),
        "duration": round(duration, 1),
        "container": (fmt.get("format_name") or "").split(",")[0],
    }


def score_of(rec, cfg):
    w, h = rec["width"], rec["height"]
    long_side, short_side = max(w, h), min(w, h)
    br = rec.get("vbitrate") or rec.get("bitrate") or 0
    fps = rec["fps"] or 25.0
    name_l = rec["name"].lower()
    score = 100
    flags = []
    why = []
    if long_side and long_side < 720:
        score -= 55
        flags.append("A")
        why.append("SD 分辨率 %dx%d" % (w, h))
    elif long_side and long_side < cfg["min_width"]:
        score -= 35
        flags.append("A")
        why.append("长边 %dpx 低于 %d" % (long_side, cfg["min_width"]))
    bpp = 0.0
    if w and h and fps and br:
        bpp = br / float(w * h * fps)
        if bpp < cfg["bpp_min"]:
            score -= 25
            flags.append("B")
            why.append("像素码率过低 %.3f bpp" % bpp)
    if br:
        hd = long_side >= 1600
        limit = cfg["br_1080"] if hd else cfg["br_720"]
        if br < limit:
            score -= 20 if hd else 15
            if "B" not in flags:
                flags.append("B")
            why.append("%s 码率仅 %.1f Mbps" % ("1080p" if hd else "720p", br / 1e6))
    if rec["vcodec"] in OLD_CODECS:
        flags.append("C")
        # 老编码在高分辨率下几乎必然是"低分辨率放大+重编码"，惩罚要随分辨率加重
        if long_side >= 1600:
            score -= 45
            why.append("老旧编码 %s @%dpx（基本是低分辨率拉伸重编码）" % (rec["vcodec"], long_side))
        elif long_side >= 1000:
            score -= 30
            why.append("老旧编码 %s @%dpx" % (rec["vcodec"], long_side))
        else:
            score -= 15
            why.append("老旧编码 %s" % rec["vcodec"])
    for kw in LOWQ_WORDS:
        if kw.isascii():
            if re.search(r"(^|[^a-z])" + kw + r"([^a-z]|$)", name_l):
                score -= 25
                flags.append("D")
                why.append("文件名疑似低质源（%s）" % kw)
                break
        elif kw in name_l:
            score -= 25
            flags.append("D")
            why.append("文件名疑似低质源（%s）" % kw)
            break
    if "10" in (rec["pix_fmt"] or ""):
        score += 3
        why.append("10bit")
    score = max(0, min(100, score))
    return score, "".join(sorted(set(flags))), "; ".join(why), round(bpp, 4)


TAG_RE = re.compile(
    r"[\.\[\]\(\)\-_ ]+(2160p|1080p|1080i|720p|576p|480p|4k|uhd|"
    r"bluray|blu-ray|web-?dl|webrip|hdtv|dvdrip|bdrip|remux|"
    r"x264|x265|h\.?264|h\.?265|hevc|avc|10bit|8bit|hdr|sdr|dovi|"
    r"aac|ac3|dts(-hd)?|truehd|flac|atmos|ddp?5\.1|2audio|3audio|"
    r"hami|dreamhd|quickio|ntb|wiki|frds|beast|chd|hdchina)\b", re.I)


def group_key(name):
    base = os.path.splitext(name)[0]
    base = re.sub(r"[\[\{【].*?[\]\}】]", " ", base)
    base = TAG_RE.sub(" ", base)
    base = re.sub(r"[-_. ]+", " ", base).strip().lower()
    base = re.sub(r"\s+", " ", base)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


DUP_REASON = "同片存在更高画质版本"


def mark_dups(conn):
    """Reset E flags/reasons, then flag every non-best copy inside a same-title group."""
    for r in conn.execute("SELECT id,flags,reason FROM files").fetchall():
        flags = (r["flags"] or "").replace("E", "")
        reason = "; ".join([x for x in (r["reason"] or "").split("; ") if x and x != DUP_REASON])
        conn.execute("UPDATE files SET flags=?, reason=? WHERE id=?", (flags, reason, r["id"]))
    groups = conn.execute("SELECT DISTINCT grp FROM files WHERE grp IS NOT NULL AND grp<>''").fetchall()
    for (g,) in groups:
        rows = conn.execute("SELECT id,score,total_score,width,bitrate,flags,reason FROM files "
                            "WHERE grp=? AND state NOT IN ('deleted','removed')", (g,)).fetchall()
        if len(rows) < 2:
            continue
        best = sorted(rows, key=lambda r: (r["total_score"] if r["total_score"] is not None else (r["score"] or 0),
                                          r["width"] or 0, r["bitrate"] or 0), reverse=True)
        for r in best[1:]:
            flags = (r["flags"] or "") + "E"
            reason = r["reason"] or ""
            reason = (reason + "; " + DUP_REASON) if reason else DUP_REASON
            conn.execute("UPDATE files SET flags=?, reason=? WHERE id=?", (flags, reason, r["id"]))


# ---------------- 画面体检：抽帧客观指标 ----------------
FRAME_W, FRAME_H = 512, 288
Q_LABELS = {"n": "噪点重", "k": "块效应", "f": "疑似拉伸", "s": "画面糊"}


_FILTER_CACHE = {}


def has_filter(name):
    if name in _FILTER_CACHE:
        return _FILTER_CACHE[name]
    ok = False
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-filters"], capture_output=True, timeout=20)
        ok = (name.encode() in (r.stdout or b""))
    except Exception:
        ok = False
    _FILTER_CACHE[name] = ok
    return ok


def _gray_frame(path, t, w, h, transfer=""):
    vf = "crop=%d:%d" % (w, h)
    hdr = (transfer or "").lower()
    if hdr in ("smpte2084", "arib-std-b67") and has_filter("zscale"):
        # HDR(PQ/HLG) → SDR 后再取灰度，否则暗部死黑会被误判成"拉伸/糊"
        tin = "smpte2084" if hdr == "smpte2084" else "arib-std-b67"
        vf += (",zscale=tin=%s:min=bt2020nc:pin=bt2020:t=bt709:m=bt709:r=tv" % tin)
    elif hdr in ("smpte2084", "arib-std-b67"):
        vf += ",eq=gamma=1.8:saturation=0"     # 没有 zscale 时的粗略提亮兜底
    vf += ",format=gray"
    cmd = [FFMPEG, "-v", "error", "-ss", "%.2f" % t, "-i", path, "-frames:v", "1",
           "-vf", vf, "-f", "rawvideo", "-"]
    out = subprocess.run(cmd, capture_output=True, timeout=90)
    buf = out.stdout
    if len(buf) < w * h:
        return None
    return buf[:w * h]


def _frame_metrics(buf, w, h):
    import math
    # 锐度：拉普拉斯方差（越大越锐）
    s = s2 = 0.0
    n = 0
    for y in range(1, h - 1, 2):
        row = y * w
        for x in range(1, w - 1, 2):
            c = buf[row + x]
            lap = 4 * c - buf[row + x - 1] - buf[row + x + 1] - buf[row - w + x] - buf[row + w + x]
            s += lap
            s2 += lap * lap
            n += 1
    m = s / max(1, n)
    sharp = s2 / max(1, n) - m * m
    # 噪点：8x8 块标准差，取最低 30% 的均值（平坦区高频能量）
    stds = []
    for by in range(0, h - 7, 8):
        for bx in range(0, w - 7, 8):
            t = t2 = 0.0
            for yy in range(by, by + 8):
                row = yy * w
                for xx in range(bx, bx + 8):
                    v = buf[row + xx]
                    t += v
                    t2 += v * v
            mm = t / 64.0
            stds.append(max(0.0, t2 / 64.0 - mm * mm) ** 0.5)
    stds.sort()
    k = max(1, int(len(stds) * 0.3))
    noise = sum(stds[:k]) / k
    # 块效应：8 像素网格边界梯度 / 非边界梯度
    eb = nb = eo = no = 0
    for y in range(1, h - 1, 2):
        row = y * w
        for x in range(8, w - 8):
            d = abs(buf[row + x] - buf[row + x - 1])
            if x % 8 == 0:
                eb += d
                nb += 1
            else:
                eo += d
                no += 1
    blk = (eb / max(1, nb)) / max(1e-6, (eo / max(1, no)))
    # 细节保留：降采样 2 倍再放大，与原图 PSNR（越高 = 越缺真实细节 = 疑似拉伸）
    hw, hh = w // 2, h // 2
    small = bytearray(hw * hh)
    for y in range(hh):
        o = (y * 2) * w
        so = y * hw
        for x in range(hw):
            i = o + x * 2
            small[so + x] = (buf[i] + buf[i + 1] + buf[i + w] + buf[i + w + 1]) >> 2
    mse = 0.0
    for y in range(h):
        row = y * w
        srow = (y // 2) * hw
        for x in range(w):
            d = buf[row + x] - small[srow + (x // 2)]
            mse += d * d
    mse /= float(w * h)
    psnr = 10 * math.log10(255.0 * 255.0 / max(mse, 1e-9))
    return {"sharp": round(sharp, 1), "noise": round(noise, 2), "blk": round(blk, 3),
            "detail_psnr": round(psnr, 2)}


def inspect_file(path, w, h, duration, cfg=None, size=0, transfer=""):
    cfg = cfg or DEFAULT_CFG
    cw = min(FRAME_W, (int(w) // 2) * 2)
    ch = min(FRAME_H, (int(h) // 2) * 2)
    if cw < 64 or ch < 64:
        return None
    dur = float(duration or 0)
    n = int(cfg.get("q_frames") or 3)
    n = max(1, min(20, n))
    if n == 3:
        pts = [dur * 0.15, dur * 0.5, dur * 0.85]      # 用户指定：15% / 50% / 85%
    elif n == 1:
        pts = [dur * 0.5]
    else:
        pts = [dur * (0.15 + 0.7 * i / (n - 1)) for i in range(n)]
    if dur <= 1:
        pts = [0.0] * n
    frames = [f for f in (_gray_frame(path, max(0.0, t), cw, ch, transfer) for t in pts) if f]
    if not frames:
        return None
    ms = [_frame_metrics(f, cw, ch) for f in frames]
    agg = {}
    for key in ms[0]:
        vals = sorted(x[key] for x in ms)
        agg[key] = vals[len(vals) // 2]
    return agg


def quality_score(m, cfg=None):
    """抽帧指标 -> 0-100 画面分 + 标签（n 噪点 k 块效应 f 疑似拉伸 s 画面糊）"""
    cfg = cfg or DEFAULT_CFG
    if not m:
        return None, "", "抽帧失败"
    score = 100
    flags = []
    why = []
    if m["noise"] >= cfg["q_noise_high"]:
        score -= 30
        flags.append("n")
        why.append("噪点很重 σ=%.2f" % m["noise"])
    elif m["noise"] >= cfg["q_noise_mid"]:
        score -= 15
        flags.append("n")
        why.append("噪点偏重 σ=%.2f" % m["noise"])
    if m["blk"] >= cfg["q_blk_high"]:
        score -= 30
        flags.append("k")
        why.append("块效应明显 %.2f" % m["blk"])
    elif m["blk"] >= cfg["q_blk_mid"]:
        score -= 14
        flags.append("k")
        why.append("轻微块效应 %.2f" % m["blk"])
    if m["detail_psnr"] >= cfg["q_psnr_high"]:
        score -= 40
        flags.append("f")
        why.append("疑似拉伸/几乎无细节 PSNR=%.1fdB" % m["detail_psnr"])
    elif m["detail_psnr"] >= cfg["q_psnr_mid"]:
        score -= 20
        flags.append("f")
        why.append("细节偏少 PSNR=%.1fdB" % m["detail_psnr"])
    if m["sharp"] <= cfg["q_sharp_low"]:
        score -= 25
        flags.append("s")
        why.append("画面很糊 锐度=%.1f" % m["sharp"])
    elif m["sharp"] <= cfg["q_sharp_mid"]:
        score -= 12
        flags.append("s")
        why.append("画面偏软 锐度=%.1f" % m["sharp"])
    return max(0, min(100, score)), "".join(sorted(set(flags))), "; ".join(why)


# ---------------- 画面体检 v2：ffmpeg 原生 QC 滤镜 ----------------
# 关键修正：不缩放、不转灰度，直接在原生像素网格上裁中心区域测量。
# blurdetect 量的是"边缘宽度(px)"（内容无关），blockdetect 需要原始 8x8 网格才准。
QC_VERSION = 5


def _qc_crop(w, h):
    cw = min(int(w or 0), 1280)
    ch = min(int(h or 0), 720)
    cw = max(64, (cw // 8) * 8)
    ch = max(64, (ch // 8) * 8)
    return cw, ch


def qc_frame(path, t, cw, ch, transfer="", want_block=True, use_gpu=False):
    """一次 ffmpeg 同时产出：统计指标(metadata) + 灰度原始像素(rawvideo)。
       相比原来两次解码，4K 上快约 1.9 倍。返回 (metrics, gray_bytes)。"""
    hdr = (transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte428")
    meta = "/tmp/mqmeta_%d_%.3f.txt" % (os.getpid(), t)
    parts = ["[0:v]crop=%d:%d:(iw-%d)/2:(ih-%d)/2,format=gray,split=2[a][b]" % (cw, ch, cw, ch)]
    if want_block and not hdr:
        # HDR 不做块效应统计（PQ 域暗部台阶会误判），也就不跑 blockdetect
        parts.append("[a]blockdetect=period_min=8:period_max=24:planes=1,signalstats,"
                     "metadata=print:file=%s[v1]" % meta)
    else:
        parts.append("[a]signalstats,metadata=print:file=%s[v1]" % meta)
    # 只解关键帧 + 不精确 seek：4K 上 1.20s → 0.17s（7 倍），
    # 落点会落到 GOP 边界（偏几秒），对"解析力"测量影响在噪声范围内（实测 −11%~+4%）
    # probesize/analyzeduration 压到最小：每帧都是一次独立进程，默认探测会重复扫文件头，
    # 实测 16.9GB 的大 MP4 上 16 帧从 24.1s 降到 5.4s（4.5 倍），画面数据完全一致
    # -threads 1：高并发时避免多进程抢核心，再省约 20%
    cmd = [FFMPEG, "-v", "error", "-noaccurate_seek", "-skip_frame", "nokey",
           "-probesize", "32", "-analyzeduration", "0", "-threads", "1"]
    if use_gpu and GPU_OK:
        cmd += ["-hwaccel", "cuda"]
    cmd += ["-ss", "%.2f" % max(0.0, t), "-i", path, "-filter_complex", ";".join(parts),
            "-map", "[v1]", "-frames:v", "1", "-f", "null", "-",
            "-map", "[b]", "-frames:v", "1", "-f", "rawvideo", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None, None
    out = {}
    try:
        with open(meta, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
    except Exception:
        lines = []
    finally:
        try:
            os.remove(meta)
        except Exception:
            pass
    for line in lines:
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k == "lavfi.block":
            key = "block"
        elif k.startswith("lavfi.signalstats."):
            key = k.rsplit(".", 1)[1].lower()
        else:
            continue
        try:
            val = float(v)
        except Exception:
            continue
        if val == val:
            out[key] = round(val, 3)
    buf = p.stdout or b""
    if len(buf) < cw * ch:
        buf = None
    if not out and buf is None:
        return None, None
    return (out if ("block" in out or "yavg" in out) else None), buf


def gpu_available():
    """探测 NVIDIA 硬解是否可用（只探一次）"""
    try:
        r = subprocess.run([FFMPEG, "-hide_banner", "-hwaccels"], capture_output=True, timeout=10)
        return b"cuda" in (r.stdout or b"").lower()
    except Exception:
        return False


GPU_OK = gpu_available()


def _raw_gray(path, t, cw, ch, transfer=""):
    """取一帧原生灰度像素（供感知模糊计算）"""
    pre = "crop=%d:%d:(iw-%d)/2:(ih-%d)/2" % (cw, ch, cw, ch)
    hdr = (transfer or "").lower()
    if hdr in ("smpte2084", "arib-std-b67") and has_filter("zscale"):
        tin = "smpte2084" if hdr == "smpte2084" else "arib-std-b67"
        pre += ",zscale=tin=%s:min=bt2020nc:pin=bt2020:t=bt709:m=bt709:r=tv" % tin
    cmd = [FFMPEG, "-v", "error", "-ss", "%.2f" % max(0.0, t), "-i", path, "-frames:v", "1",
           "-vf", pre + ",format=gray", "-f", "rawvideo", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception:
        return None
    b = p.stdout or b""
    return b[:cw * ch] if len(b) >= cw * ch else None


def percept_metrics(buf, cw, ch, step=2, hdr=False):
    """感知模糊度：在原生像素上量「边缘 10%→90% 上升距离」并扣除锐化过冲。
       返回 (上升距离px, 过冲比)；与人工判断相关系数 r≈0.64（实测 17 样本）。"""
    if not buf:
        return None
    edges = []
    for y in range(1, ch - 1, step):
        r0, r1, r2 = (y - 1) * cw, y * cw, (y + 1) * cw
        for x in range(2, cw - 2, step):
            gx = (buf[r0 + x + 1] + 2 * buf[r1 + x + 1] + buf[r2 + x + 1]
                  - buf[r0 + x - 1] - 2 * buf[r1 + x - 1] - buf[r2 + x - 1])
            gy = (buf[r2 + x - 1] + 2 * buf[r2 + x] + buf[r2 + x + 1]
                  - buf[r0 + x - 1] - 2 * buf[r0 + x] - buf[r0 + x + 1])
            ax, ay = abs(gx), abs(gy)
            if ax >= ay:
                if ax >= 25:
                    edges.append((ax, x, y, "x"))
            elif ay >= 25:
                edges.append((ay, x, y, "y"))
    if len(edges) < 30:
        return None
    edges.sort(reverse=True)
    rises, overs = [], []

    def cross(prof, th):
        for i in range(1, len(prof)):
            a, b = prof[i - 1], prof[i]
            if (a - th) * (b - th) <= 0 and a != b:
                return (i - 1) + (th - a) / (b - a)
        return None

    # 取样改为「12×7 网格均衡」：每格取前 15 条最强边。
    # 只取全局最强的 1200 条会偏向"纹理最密的地方"，纹理越多分越高（实测差 0.2~1.1）
    cells = {}
    for e in edges:
        key = (e[1] * 12 // max(1, cw), e[2] * 7 // max(1, ch))
        lst = cells.get(key)
        if lst is None:
            cells[key] = [e]
        elif len(lst) < 15:
            lst.append(e)
    picked = [e for v in cells.values() for e in v] or edges[:1200]
    for _m, x, y, axis in picked:
        prof = []
        for k in range(-6, 7):
            xx = x + (k if axis == "x" else 0)
            yy = y + (k if axis == "y" else 0)
            if 0 <= xx < cw and 0 <= yy < ch:
                prof.append(buf[yy * cw + xx])
        if len(prof) < 13:
            continue
        lo, hi = min(prof), max(prof)
        if hi - lo < 25:
            continue
        i10 = cross(prof, lo + 0.1 * (hi - lo))
        i90 = cross(prof, lo + 0.9 * (hi - lo))
        if i10 is None or i90 is None:
            continue
        d = abs(i90 - i10)
        if d <= 0.05:
            continue
        rises.append(d)
        if hdr:
            continue                     # HDR 色调映射会产生假高光，不参与过冲统计
        sv = sorted(prof)
        p_lo, p_hi = sv[1], sv[-2]       # 用次外值当平台，避开单点噪声
        top, bot = max(p_lo, p_hi), min(p_lo, p_hi)
        amp = max(1.0, top - bot)
        peak_hi, peak_lo = sv[int(len(sv) * 0.9)], sv[int(len(sv) * 0.1)]
        over = (max(0.0, peak_hi - top) + max(0.0, bot - peak_lo)) / amp
        overs.append(min(1.5, over))     # 截断，防止颗粒/振铃把指数拉爆
    if not rises:
        return None
    rises.sort()
    overs.sort()
    return rises[len(rises) // 2], (overs[len(overs) // 2] if overs else 0.0)


def tier_cal(height=0):
    """按档位的指数校准系数 —— 现已统一为 1.0（不再分档校准）。
       原因：下面的锚点是用 18 个人工打分样本直接标定出来的，
       分档校准与标定曲线叠加会导致“双重校正”，故保留函数仅为兼容旧调用。"""
    return 1.0


def _percept_score(idx, height=0):
    """模糊指数 → 0~100 分（按 18 个人工打分样本做保序回归标定，统一一条曲线）。
       样本对应关系：0.99→80 ｜ 1.32→100 ｜ 1.37→100 ｜ 1.46→100
                     1.91→70 ｜ 2.07→30 ｜ 2.30→100 ｜ 2.82→35
                     3.31→60 ｜ 4.20→20 ｜ 5.67→20
       单变量与人工分相关系数 r = −0.716，MAE ≈ 12 分（旧曲线 16.9）。"""
    pts = [(0.8, 97.0), (1.0, 96.0), (1.2, 94.0), (1.6, 88.0), (2.0, 72.0),
           (2.4, 68.0), (2.8, 50.0), (3.2, 38.0), (3.4, 33.0), (4.2, 20.0),
           (5.7, 18.0)]
    x = float(idx)
    if x <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-6, (x1 - x0))
    return 0.0


def qc_inspect(path, w, h, duration, cfg=None, size=0, transfer=""):
    """抽 n 帧（默认 15%/50%/85%），跳过黑场/纯色帧，返回中位数指标。"""
    cfg = cfg or DEFAULT_CFG
    cw, ch = _qc_crop(w, h)
    if cw < 64 or ch < 64:
        return None
    dur = float(duration or 0)
    n = int(cfg.get("q_frames") or 3)
    n = max(1, min(20, n))
    if dur <= 1:
        pts = [0.0] * n
    elif n == 3:
        pts = [dur * 0.15, dur * 0.5, dur * 0.85]
    elif n == 1:
        pts = [dur * 0.5]
    elif n == 5:
        # 保留用户指定的 15/50/85，另补 30/70 两帧，降低"抽到昏暗特写"的偶然性
        pts = [dur * 0.15, dur * 0.30, dur * 0.50, dur * 0.70, dur * 0.85]
    else:
        pts = [dur * (0.15 + 0.7 * i / (n - 1)) for i in range(n)]

    frames = []
    bright = []
    _is_hdr = (transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte428")
    _gpu = bool(cfg.get("use_gpu")) and GPU_OK
    for t in pts:
        m, buf = qc_frame(path, t, cw, ch, transfer, True, _gpu)
        if not m or not buf:
            continue
        if m.get("ymax", 255) - m.get("ymin", 0) < 8:
            continue            # 纯色帧
        # 感知模糊（原生像素）：同一次解码拿到的灰度像素，不再二次调用 ffmpeg
        pm = percept_metrics(buf, cw, ch, 2, _is_hdr)
        if pm:
            rise, over = pm
            m["rise"] = round(rise, 3)
            m["over"] = round(over, 3)
            m["bluridx"] = round(rise / max(1.0, float(w or cw)) * 1000.0 + over, 3)
        frames.append(m)
        if (m.get("yavg") or 0) >= 30:
            bright.append(m)
    if not frames:
        return None
    # 暗场帧会放大 8×8 量化台阶，但只对 SDR 有效：HDR(PQ) 的 Y 不是线性亮度，
    # 相对阈值会把大量正常帧误删（Black Widow 曾被删到只剩 2 帧）→ HDR 不做亮度筛选
    _hdr_any = ((transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte428")
                or any(f.get("_hdr") for f in frames))
    if _hdr_any or len(frames) <= 3:
        rel = frames
    else:
        # 取"最亮的若干帧"，而不是设亮度阈值——整片偏暗时阈值会把帧删到只剩 1 帧
        srt = sorted(frames, key=lambda f: (f.get("yavg") or 0), reverse=True)
        keep_n = max(3, int(round(len(srt) * 0.6)))
        rel = srt[:keep_n]
    use = rel or bright or frames
    # 逐键收集（部分帧算不出 bluridx/rise 时不能按"第一帧的键"去索引，曾因此整条体检失败）
    agg = {}
    keys = set()
    for f in use:
        keys |= set(f.keys())
    for k in keys:
        vals = sorted(f[k] for f in use if k in f)
        if vals:
            agg[k] = vals[len(vals) // 2]
    # 清晰度用「全部帧的 p25」（偏锐段）：模糊是单向的——运动模糊/失焦只会更糊，
    # 不会让真正糊的编码变清晰，所以取最清楚的几帧才代表这片的实际解析力
    for k in ("bluridx", "over", "rise"):
        vals = sorted(f[k] for f in frames if k in f)
        if vals:
            i = int(round((len(vals) - 1) * 0.20))
            agg[k] = vals[i]
    agg["_frames"] = len(frames)
    agg["_crop"] = "%dx%d" % (cw, ch)
    agg["_hdr"] = 1 if (transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte428") else 0
    return agg


def _blur_score(blur, cfg, height=0, width=0):
    """边缘宽度 → 0~100 的连续分（分段线性）。
       低分辨率（宽<1000）按相对宽度换算：320 宽的画面里 6.5px 边缘 = 1080p 的 26px，非常糊。
       4K/UHD 片源在 1:1 下边缘天然宽，用独立锚点，否则好片会被误判。"""
    w = int(width or 0)
    if w and w < 1000:
        blur = float(blur) * (1280.0 / w)
    if int(height or 0) >= 1600:
        # 4K UHD 多为 2K DI 上采样，原生像素下 blur 6~9px 属正常水平，锚点需放宽
        pts = [(3.0, 100.0), (6.0, 92.0), (9.0, 80.0), (13.0, 60.0),
               (18.0, 35.0), (24.0, 10.0), (30.0, 0.0)]
    else:
        pts = [(cfg["q_blur_floor"], 100.0),
               (cfg["q_blur_soft"], 80.0),
               (cfg["q_blur_mid"], 58.0),
               (cfg["q_blur_bad"], 32.0),
               (cfg["q_blur_bad"] * 1.55, 0.0)]
    x = float(blur)
    if x <= pts[0][0]:
        return pts[0][1]
    for i in range(1, len(pts)):
        x0, y0 = pts[i - 1]
        x1, y1 = pts[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-6, (x1 - x0))
    return 0.0


def qc_score(m, cfg=None, height=0, transfer="", width=0):
    """画面分 v2：只惩罚"肉眼看得出来"的缺陷；胶片颗粒/高 ISO 噪点不再扣分。
       4K/UHD 片源特殊处理：胶片扫描在 1:1 下边缘天然偏宽，blur 指标会严重低估，
       因此对 height>=1600 的画面分给一个下限，避免好片子被判"建议清理"。"""
    cfg = cfg or DEFAULT_CFG
    # 新口径只需模糊指数；blur 仅作参考（HDR/暗场帧可能没有 blur）
    if not m or ("bluridx" not in m and "blur" not in m):
        return None, "", "抽帧失败"
    flags = []
    why = []
    blur = m.get("blur")
    bi = m.get("bluridx")
    if bi is not None:
        # 统一曲线（不再分档校准，锚点已用人工样本直接标定）
        score = _percept_score(bi, height)
        t_bad, t_soft, t_ok = 3.6, 2.6, 2.0
        if bi >= t_bad:
            flags.append("f")
            why.append("感知模糊指数 %.2f（明显模糊/拉伸）" % bi)
        elif bi >= t_soft:
            flags.append("f")
            why.append("感知模糊指数 %.2f（偏软）" % bi)
        elif bi >= t_ok:
            flags.append("s")
            why.append("感知模糊指数 %.2f（略软）" % bi)
        else:
            why.append("感知模糊指数 %.2f（清晰）" % bi)
    else:
        score = 100 if blur is None else _blur_score(blur, cfg, height, width)
        if blur is not None:
            if blur >= cfg["q_blur_bad"]:
                flags.append("f")
                why.append("边缘宽度 %.1fpx，明显模糊" % blur)
            elif blur >= cfg["q_blur_soft"]:
                flags.append("s")
                why.append("边缘宽度 %.1fpx，偏软" % blur)
    # HDR(PQ/HLG) 的 Y 值不在 bt709 域，亮/暗/对比/块效应判据全部不适用，直接跳过
    hdr = (transfer or "").lower() in ("smpte2084", "arib-std-b67", "smpte428") or m.get("_hdr")
    blk = m.get("block")
    if blk is not None and not hdr:
        # 块效应改为独立分项（见 score3），这里只出标签
        if blk >= cfg["q_block_bad"]:
            flags.append("k")
            why.append("压缩块效应明显 %.2f" % blk)
        elif blk >= cfg["q_block_mid"]:
            flags.append("k")
            why.append("有压缩块效应 %.2f" % blk)
    elif blk is not None:
        why.append("HDR 片源，块效应 %.2f 仅作参考（不计分）" % blk)
    # 亮/暗/对比判据（仅 SDR）
    yavg = m.get("yavg")
    yavg = m.get("yavg")
    if not hdr:
        # 亮度/对比度只出标签，不参与打分（综合评分 = 清晰度 − 块效应扣分）
        lo, hi = m.get("ylow"), m.get("yhigh")
        if (lo is not None and hi is not None and (hi - lo) < cfg["q_flat"]
                and (yavg is None or yavg < cfg["q_flat_yavg"])):
            flags.append("c")
            why.append("对比度偏低 Y%d~%d（均值%.0f，仅提示）" % (lo, hi, yavg or 0))
    return int(round(max(0, min(100, score)))), "".join(sorted(set(flags))), "; ".join(why)


# 块效应 0~100 分（数值越大分越低）。由用户样本校准：1.9→72、2.0→58、3.3→18
BLOCK_PTS = [(1.2, 100.0), (1.5, 90.0), (1.8, 72.0), (2.2, 45.0), (3.0, 18.0), (4.2, 0.0)]


def block_score_of(bk):
    if bk is None:
        return 100.0
    x = float(bk)
    if x <= BLOCK_PTS[0][0]:
        return BLOCK_PTS[0][1]
    for i in range(1, len(BLOCK_PTS)):
        x0, y0 = BLOCK_PTS[i - 1]
        x1, y1 = BLOCK_PTS[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-6, (x1 - x0))
    return 0.0


# 块效应扣分（只扣不加）
BLOCK_PEN = [(1.2, 0.0), (1.6, 5.0), (2.1, 12.0), (3.0, 22.0), (4.2, 32.0), (8.0, 45.0)]


def block_penalty_of(bk):
    if bk is None:
        return 0.0
    x = float(bk)
    if x <= BLOCK_PEN[0][0]:
        return BLOCK_PEN[0][1]
    for i in range(1, len(BLOCK_PEN)):
        x0, y0 = BLOCK_PEN[i - 1]
        x1, y1 = BLOCK_PEN[i]
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / max(1e-6, (x1 - x0))
    return 45.0


def add_upscale_flag(qf, notes):
    """score3 判定"疑似拉伸→分辨率降档"时，补一个 p（假高清）标签。"""
    if any("拉伸" in n for n in (notes or [])):
        return "".join(sorted(set((qf or "") + "p")))
    return qf or ""


def bpp_adjust(w, h, vbitrate, fps, clar, cfg):
    """bpp（每像素比特）调整分：只防“欠码率”，加分需过护栏。
       bpp = 码率 ÷ (宽 × 高 × min(帧率,30))   ← 60fps 折算到 30，避免被误判欠码率
       <0.05 → −10 ｜ 0.05~0.10 → 0
       0.10~0.15 → +3 ｜ >0.15 → +6（两者都要求 高≥1000 且 清晰度≥60）"""
    if not int(cfg.get("bpp_adjust_on", 1) or 0):
        return 0.0
    _fps = max(1.0, min(float(fps or 0) or 24.0, 30.0))
    bpp = (vbitrate or 0) / max(1.0, float(w or 1) * float(h or 1) * _fps)
    if bpp <= 0:
        return 0.0
    if bpp < 0.05:
        return -10.0
    if bpp <= 0.10:
        return 0.0
    if int(h or 0) < 1000 or clar < 60:
        return 0.0
    if bpp <= 0.15:
        return 3.0
    return 6.0


def score3(w, h, vbitrate, clarity, flags, cfg, bk=None, qflags="", fps=0, hdr=0):
    """四分量加权：分辨率 + 码率 + 清晰度(略软/模糊) + 块效应，再套硬约束。
       返回 (综合分, 分辨率分, 码率分, 清晰度分, 结论, 备注列表)"""
    tier = res_tier(w, h)
    clar = 70.0 if clarity is None else float(clarity)
    # 块效应是否参与扣分由开关控制（默认关：实测与人工打分无相关性，仅作标签）
    _pen_on = int(cfg.get("block_penalty_on", 0) or 0)
    pen = 0.0 if (hdr or not _pen_on) else block_penalty_of(bk)
    bpp_adj = bpp_adjust(w, h, vbitrate, fps, clar, cfg)
    notes = []
    # 综合评分 = 清晰度分 + bpp 调整 − 块效应扣分。分辨率权重、码率加分、封顶均不参与。
    total = int(round(max(0.0, min(100.0, clar + bpp_adj - pen))))
    if pen > 0:
        notes.append("块效应扣 %.1f 分" % pen)
    if abs(bpp_adj) >= 0.5:
        notes.append("欠码率/码率密度 调整 %+.0f 分" % bpp_adj)
    vd = "bad" if total < cfg["verdict_bad"] else "ok"
    return total, 0, 0, int(round(clar)), vd, notes


def total_of(param, q, cfg):
    """综合分 = 画面为主(0.75) + 参数(0.25)；
       参数分过低 → 直接以参数分为上限；画面分过低 → 压到 q_floor_cap 以下（保证落入"建议清理"）"""
    param = param or 0
    if q is None:
        return int(param)
    t = param * float(cfg["w_param"]) + q * float(cfg["w_quality"])
    if param < float(cfg["param_floor"]):
        t = min(t, param)
    if q < float(cfg["q_floor"]):
        t = min(t, float(cfg["q_floor_cap"]))
    return int(round(max(0, min(100, t))))


def verdict_of(param, q, total, cfg, flags=""):
    """结论：clean 建议清理 / replace 建议换源 / keep 可保留 / good 优质
       顺序很关键：先判"极差→清理"，再判"参数好画面差→换源"，
       否则因为参数分普遍很高，所有差片都会落进"换源"，"清理"永远为空。"""
    if total < cfg["verdict_clean"]:
        return "clean"
    # 老编码(如 mpeg4/xvid)跑 1080p：基本是低分辨率拉伸重编码，直接建议换源
    if "C" in (flags or "") and total < cfg["verdict_good"]:
        return "replace"
    if q is not None and (param or 0) >= cfg["suspect_param"] and q < cfg["suspect_quality"]:
        return "replace"
    if total >= cfg["verdict_good"] and (q is None or q >= cfg["good_quality"]):
        return "good"
    return "keep"


def _inspect_worker(r, cfg):
    """单个文件的体检（线程内执行，不碰数据库）"""
    try:
        m = qc_inspect(r["path"], r["width"] or 0, r["height"] or 0, r["duration"] or 0,
                       cfg, r["size"] or 0, (r["ctransfer"] or ""))
        q, qf, qw = qc_score(m, cfg, r["height"] or 0, r["ctransfer"] or "", r["width"] or 0)
    except Exception as e:
        m, q, qf, qw = None, None, "", "体检失败: %s" % e
    total, rs, bs, clar, vd, notes = score3(r["width"], r["height"], r["vbitrate"],
                                            q, r["flags"], cfg, (m or {}).get("block"), qf,
                                            r["fps"] or 0,
                                            1 if (m or {}).get("_hdr") else 0)
    if notes:
        qw = (qw + "; " if qw else "") + "; ".join(notes)
        qf = add_upscale_flag(qf, notes)
    return q, qf, qw, m, total, rs, bs, clar, vd


def run_inspect(ids=None, force=False):
    # 关键：上一次点过"停止"会把 JOB_STOP 置位，这里必须清掉，
    # 否则后续每次体检都会在 0 个文件时立刻退出（表现为"点了几秒钟就结束"）
    JOB_STOP.clear()
    conn = STORE.db()
    if ids:
        marks = ",".join("?" * len(ids))
        rows = conn.execute("SELECT id,path,width,height,duration,size,score,flags,ctransfer,vbitrate,fps FROM files WHERE id IN (%s)" % marks,
                            ids).fetchall()
    elif force:
        rows = conn.execute("SELECT id,path,width,height,duration,size,score,flags,ctransfer,vbitrate,fps FROM files "
                            "WHERE state NOT IN ('deleted','removed')").fetchall()
    else:
        rows = conn.execute("SELECT id,path,width,height,duration,size,score,flags,ctransfer,vbitrate,fps FROM files "
                            "WHERE state NOT IN ('deleted','removed') AND (q_checked IS NULL OR q_checked=0)").fetchall()
    cfg = STORE.cfg
    with LOCK:
        JOB.update(state="scanning", phase="inspect", total=len(rows), done=0, new=0, updated=0,
                   found=0, cur="", msg="", t0=now(), ended=0)
    # 并发体检：多线程跑 ffmpeg（每帧一次解码），主线程串行写库
    workers = max(1, min(6, int(cfg.get("q_concurrency") or 4)))
    n_ok = 0
    done = 0
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    try:
        futs = {ex.submit(_inspect_worker, r, cfg): r for r in rows}
        for fut in concurrent.futures.as_completed(futs):
            r = futs[fut]
            done += 1
            with LOCK:
                JOB.update(done=done, cur=os.path.basename(r["path"]))
            if JOB_STOP.is_set():
                for f in futs:
                    f.cancel()
                break
            try:
                res = fut.result()
            except Exception as e:
                res = None
                print("inspect worker error: %s" % e, flush=True)
            if not res:
                continue
            q, qf, qw, m, total, rs, bs, clar, vd = res
            conn.execute("UPDATE files SET q_score=?, q_flags=?, q_detail=?, q_checked=?, "
                         "total_score=?, verdict=?, res_score=?, br_score=?, clarity_score=? "
                         "WHERE id=?",
                         (q, qf, json.dumps({"v": QC_VERSION, "metrics": m, "why": qw},
                                            ensure_ascii=False),
                          now(), total, vd, rs, bs, clar, r["id"]))
            n_ok += 1
            if done % 10 == 0:
                conn.commit()
    finally:
        ex.shutdown(wait=False)
    conn.commit()
    conn.close()
    with LOCK:
        JOB.update(state="idle" if not JOB_STOP.is_set() else "stopped", phase="", cur="", ended=now(),
                   msg=("画面体检已停止（完成 %d 个）" % n_ok) if JOB_STOP.is_set()
                   else ("画面体检完成：%d 个文件" % n_ok))


_HW_ENC = {"name": None, "checked": 0}


def hw_encoder():
    """探测可用的 H.264 编码器：优先 NVIDIA NVENC，失败退回 libx264。
    注意：NVENC 有最小分辨率限制（128x128 会报 dimension too small），必须用正常尺寸探测。"""
    import time as _t
    if _HW_ENC["name"] and (_t.time() - _HW_ENC["checked"] < 600):
        return _HW_ENC["name"]
    name = "libx264"
    args = [FFMPEG, "-v", "error", "-f", "lavfi", "-i", "color=size=640x360:rate=10",
            "-frames:v", "2", "-vf", "format=yuv420p", "-pix_fmt", "yuv420p",
            "-c:v", "h264_nvenc", "-f", "null", "-"]
    try:
        r = subprocess.run(args, capture_output=True, timeout=20)
        if r.returncode == 0:
            name = "h264_nvenc"
        else:
            err = (r.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
            print("nvenc probe failed: %s" % (err[:1] or "unknown"), flush=True)
    except Exception as e:
        print("nvenc probe error: %s" % e, flush=True)
    _HW_ENC["name"] = name
    _HW_ENC["checked"] = _t.time()
    print("video encoder selected: %s" % name, flush=True)
    return name


def walk_videos(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("@eaDir", "#recycle")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            if is_media(fn):
                yield os.path.join(dirpath, fn)


def run_scan(roots):
    cfg = STORE.cfg
    JOB_STOP.clear()
    conn = STORE.db()
    # phase 1: enumerate
    todo = []
    total = 0
    with LOCK:
        JOB.update(state="scanning", phase="enum", total=0, done=0, found=0, new=0, updated=0,
                   cur="枚举文件…", msg="", t0=now(), ended=0)
    for root in roots:
        for p in walk_videos(root):
            total += 1
            try:
                st = os.stat(p)
            except Exception:
                continue
            row = conn.execute("SELECT id,size,mtime,probed_at,state FROM files WHERE path=?",
                               (p,)).fetchone()
            if row and (row["state"] or "") == "removed":
                # 之前移出过扫描范围，现在又被扫到 → 恢复显示（体检数据直接复用）
                conn.execute("UPDATE files SET state='keep' WHERE id=?", (row["id"],))
            if row and row["probed_at"] and row["size"] == st.st_size and abs(row["mtime"] - st.st_mtime) < 1:
                continue
            todo.append((p, st.st_size, st.st_mtime, row is None))
            if total % 200 == 0:
                with LOCK:
                    JOB.update(found=total, cur="已发现 %d 个视频…" % total)
    with LOCK:
        JOB.update(phase="probe", total=len(todo), found=total, cur="探测元数据…")
    # phase 2: probe
    pool = []
    for i, (p, size, mtime, is_new) in enumerate(todo):
        if JOB_STOP.is_set():
            break
        with LOCK:
            JOB.update(done=i, cur=os.path.basename(p),
                       new=JOB["new"] + (1 if is_new else 0),
                       updated=JOB["updated"] + (0 if is_new else 1))
        try:
            info = probe_file(p)
        except Exception as e:
            conn.execute("""INSERT INTO files(path,name,dir,size,mtime,state,probed_at,reason)
                VALUES(?,?,?,?,?,'keep',?,?) ON CONFLICT(path) DO UPDATE SET
                size=excluded.size, mtime=excluded.mtime, probed_at=excluded.probed_at, reason=excluded.reason""",
                (p, os.path.basename(p), os.path.dirname(p), size, mtime, now(), "探测失败: %s" % e))
            continue
        sc, flags, why, bpp = score_of(dict(info, name=os.path.basename(p)), cfg)
        grp = group_key(os.path.basename(p))
        q = qf = qd = None
        if cfg.get("inspect_on_scan"):
            try:
                m = qc_inspect(p, info["width"], info["height"], info["duration"], cfg, size,
                               info.get("ctransfer") or "")
                q, qf, qw = qc_score(m, cfg, info["height"], info.get("ctransfer") or "",
                                     info["width"])
                qd = json.dumps({"v": QC_VERSION, "metrics": m, "why": qw}, ensure_ascii=False)
            except Exception as e:
                qd = json.dumps({"metrics": None, "why": "体检失败: %s" % e}, ensure_ascii=False)
        total, rs, bs, clar, vd, notes = score3(info["width"], info["height"], info["vbitrate"],
                                               q, flags, cfg,
                                               (m or {}).get("block") if q is not None else None, qf,
                                               info["fps"],
                                               1 if (m or {}).get("_hdr") else 0)
        if notes:
            why = (why + "; " if why else "") + "; ".join(notes)
            qf = add_upscale_flag(qf, notes)
            if cfg.get("inspect_on_scan") and q is not None:
                qd = json.dumps({"v": QC_VERSION, "metrics": m,
                                 "why": "; ".join([qw] + notes)}, ensure_ascii=False)
        conn.execute("""INSERT INTO files(path,name,dir,size,mtime,width,height,fps,bitrate,abitrate,
            vbitrate,vcodec,pix_fmt,acodec,channels,duration,container,score,flags,reason,grp,state,
            q_score,q_flags,q_detail,q_checked,total_score,verdict,ctransfer,cprimaries,
            res_score,br_score,clarity_score,probed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'keep',?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime,
            width=excluded.width, height=excluded.height, fps=excluded.fps, bitrate=excluded.bitrate,
            abitrate=excluded.abitrate, vbitrate=excluded.vbitrate,
            vcodec=excluded.vcodec, pix_fmt=excluded.pix_fmt, acodec=excluded.acodec,
            channels=excluded.channels, duration=excluded.duration, container=excluded.container,
            score=excluded.score, flags=excluded.flags, reason=excluded.reason, grp=excluded.grp,
            q_score=excluded.q_score, q_flags=excluded.q_flags, q_detail=excluded.q_detail,
            q_checked=excluded.q_checked, total_score=excluded.total_score, verdict=excluded.verdict,
            ctransfer=excluded.ctransfer, cprimaries=excluded.cprimaries,
            res_score=excluded.res_score, br_score=excluded.br_score,
            clarity_score=excluded.clarity_score, probed_at=excluded.probed_at""",
            (p, os.path.basename(p), os.path.dirname(p), size, mtime, info["width"], info["height"],
             info["fps"], info["bitrate"], info["abitrate"], info["vbitrate"], info["vcodec"],
             info["pix_fmt"], info["acodec"], info["channels"], info["duration"], info["container"],
             sc, flags, why, grp, q, qf, qd, now() if qf is not None else None, total, vd,
             info.get("ctransfer") or "", info.get("cprimaries") or "", rs, bs, clar, now()))
        if i % 25 == 0:
            conn.commit()
        pool.append(grp)
    conn.commit()
    mark_dups(conn)
    conn.commit()
    meta = {"ts": now(), "seconds": round(now() - JOB["t0"], 1), "found": JOB["found"],
            "scanned": JOB["total"], "new": JOB["new"], "updated": JOB["updated"],
            "state": "stopped" if JOB_STOP.is_set() else "done"}
    conn.execute("INSERT INTO meta(k,v) VALUES('last_scan',?) "
                 "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                 (json.dumps(meta, ensure_ascii=False),))
    conn.commit()
    conn.close()
    with LOCK:
        JOB.update(state="idle" if not JOB_STOP.is_set() else "stopped", phase="", cur="", ended=now(),
                   msg=("已停止" if JOB_STOP.is_set() else
                        "扫描完成：发现 %d 个视频，新录入 %d，更新 %d，用时 %.1f 秒"
                        % (meta["found"], meta["new"], meta["updated"], meta["seconds"])))


class UnixHTTPServer(ThreadingHTTPServer):
    """绑定 Unix domain socket，仅供飞牛统一网关转发。"""
    address_family = socket.AF_UNIX

    def server_bind(self):
        try:
            if os.path.exists(self.server_address):
                os.unlink(self.server_address)
        except Exception:
            pass
        socketserver.TCPServer.server_bind(self)
        try:
            os.chmod(self.server_address, 0o777)
        except Exception:
            pass

    def server_activate(self):
        socketserver.TCPServer.server_activate(self)


class Handler(BaseHTTPRequestHandler):
    server_version = "MediaQC/1.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ---------- 网关：前缀剥离 + 登录态 ----------
    def norm_path(self, path):
        p = (PREFIX or "").rstrip("/")
        if p and path.startswith(p):
            path = path[len(p):]
            if not path.startswith("/"):
                path = "/" + path
        return path or "/"

    def gateway_user(self):
        for h in GATEWAY_USER_HEADERS:
            v = (self.headers.get(h) or "").strip()
            if v:
                return v
        return ""

    def log_once(self):
        global AUTH_LOG_DONE
        if AUTH_LOG_DONE:
            return
        AUTH_LOG_DONE = True
        try:
            print("gateway headers: %r" % (dict(self.headers),), flush=True)
        except Exception:
            pass

    def need_login(self):
        if not REQUIRE_AUTH:
            return False
        if STORE is not None and not STORE.cfg.get("require_gateway_auth", True):
            return False
        return not self.gateway_user()

    def deny_login(self):
        self.send_json({"ok": False, "need_login": True, "error": "未登录飞牛，请先登录 fnOS"},
                       401)

    # ---------- helpers ----------
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def err(self, msg, code=400):
        self.send_json({"ok": False, "error": msg}, code)

    def body_json(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def q(self):
        u = urllib.parse.urlparse(self.path)
        return self.norm_path(u.path), urllib.parse.parse_qs(u.query)

    def check_pin(self, qs):
        pin = STORE.cfg.get("pin") or ""
        if not pin:
            return True
        given = (qs.get("pin") or [""])[0] or (self.headers.get("X-Pin") or "")
        return given == pin

    # ---------- static ----------
    def serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        full = os.path.realpath(os.path.join(WWW_DIR, rel))
        if not full.startswith(os.path.realpath(WWW_DIR)):
            return self.err("forbidden", 403)
        if not os.path.isfile(full):
            return self.err("not found", 404)
        ext = os.path.splitext(full)[1].lower()
        ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".png": "image/png", ".svg": "image/svg+xml",
                 ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if getattr(self, "head_only", False):
            return
        self.wfile.write(data)

    # ---------- GET ----------
    def do_GET(self):
        path, qs = self.q()
        self.log_once()
        try:
            if path.startswith("/api/"):
                if self.need_login():
                    return self.deny_login()
                if not self.check_pin(qs):
                    return self.err("需要访问 PIN", 401)
                return self.api_get(path, qs)
            return self.serve_static(path)
        except BrokenPipeError:
            return
        except Exception as e:
            return self.err(str(e), 500)

    def api_get(self, path, qs):
        arg = lambda k, d=None: (qs.get(k) or [d])[0]
        if path == "/api/ping":
            return self.send_json({"ok": True, "app": "媒体画质管家", "version": "1.0.0",
                                   "ffmpeg": FFMPEG, "roots": STORE.cfg["roots"]})
        if path == "/api/config":
            if arg("defaults"):
                return self.send_json({"ok": True, "config": dict(DEFAULT_CFG)})
            return self.send_json({"ok": True, "config": STORE.cfg})
        if path == "/api/roots":
            out = [{"path": r, "exists": os.path.isdir(r)} for r in STORE.cfg["roots"]]
            return self.send_json({"ok": True, "roots": out})
        if path == "/api/browse":
            p = (arg("path") or "").rstrip("/")
            if p == "/":
                p = ""
            skipped = 0
            hidden_sys = 0
            hide_sys = bool(STORE.cfg.get("hide_sys_dirs", True))
            junk = {"lost+found", "#recycle", "recycle", "@eaDir", "@__thumb", "tmp"}
            base_names = set(STORE.cfg.get("roots") or [])

            def keep(name, at_vol_root):
                """返回 True 表示显示；隐藏计数在闭包里累加不方便，这里只做判断"""
                if name.startswith("@") or name.startswith("."):
                    return False
                if name in junk:
                    return False
                if hide_sys and at_vol_root and name in SYS_DIR_NAMES:
                    return False
                return True

            if not p:
                ofs = []
                # 只列存储卷与共享目录，不罗列 Linux 根文件系统（/bin /etc /proc …）
                bases = []
                try:
                    for n in sorted(os.listdir("/")):
                        if re.fullmatch(r"vol\d+", n):
                            bases.append("/" + n)
                except Exception:
                    pass
                for extra in ("/share", "/fs"):
                    if os.path.isdir(extra):
                        bases.append(extra)
                for base in bases:
                    try:
                        names = sorted(os.listdir(base))
                    except Exception:
                        skipped += 1
                        continue
                    for d in names:
                        fp = os.path.join(base, d)
                        if not keep(d, True):
                            hidden_sys += 1
                            continue
                        try:
                            if os.path.isdir(fp):
                                ofs.append({"name": d, "path": fp})
                        except Exception:
                            skipped += 1
                return self.send_json({"ok": True, "path": "", "dirs": ofs, "up": "",
                                       "skipped": skipped, "hidden_sys": hidden_sys})
            dirs = []
            at_root = bool(VOL_ROOT_RE.match(p.rstrip("/")))
            try:
                names = sorted(os.listdir(p))
            except PermissionError:
                return self.err("无权访问 %s（应用运行用户权限不足，请改用 root 运行或选择其它目录）" % p, 403)
            except Exception as e:
                return self.err("无法读取目录: %s" % e)
            for d in names:
                if not keep(d, at_root):
                    hidden_sys += 1
                    continue
                fp = os.path.join(p, d)
                try:
                    if os.path.isdir(fp):
                        dirs.append({"name": d, "path": fp})
                except Exception:
                    skipped += 1
            up = os.path.dirname(p)
            if up in ("", "/") or up == p:
                up = ""          # 卷根目录的上一级 = 伪根（只列存储卷），不暴露 /bin /usr /sys
            return self.send_json({"ok": True, "path": p, "dirs": dirs, "up": up,
                                   "skipped": skipped, "hidden_sys": hidden_sys})
        if path == "/api/job":
            with LOCK:
                j = dict(JOB)
            elapsed = (now() - j["t0"]) if j["t0"] else 0
            j["elapsed"] = round(elapsed, 1)
            if j["state"] == "scanning" and j["done"] > 3 and elapsed > 2:
                rate = j["done"] / elapsed
                j["eta"] = round(max(0.0, (j["total"] - j["done"]) / rate), 1)
                j["per_sec"] = round(rate, 1)
            else:
                j["eta"] = None
                j["per_sec"] = None
            last = None
            try:
                conn = STORE.db()
                row = conn.execute("SELECT v FROM meta WHERE k='last_scan'").fetchone()
                conn.close()
                if row:
                    last = json.loads(row["v"])
            except Exception:
                last = None
            return self.send_json({"ok": True, "job": j, "last_scan": last})
        if path == "/api/stats":
            conn = STORE.db()
            tot = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files WHERE state NOT IN ('deleted','removed')").fetchone()
            rows = conn.execute("""SELECT CASE WHEN flags LIKE '%A%' THEN 'A' WHEN flags LIKE '%D%' THEN 'D'
                WHEN flags LIKE '%E%' THEN 'E' WHEN flags LIKE '%C%' THEN 'C' WHEN flags LIKE '%B%' THEN 'B'
                ELSE 'none' END g, COUNT(*) c, COALESCE(SUM(size),0) s
                FROM files WHERE state NOT IN ('deleted','removed') GROUP BY g""").fetchall()
            fl = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files "
                              "WHERE state NOT IN ('deleted','removed') AND flags<>''").fetchone()
            qb = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files "
                              "WHERE state NOT IN ('deleted','removed') AND q_score IS NOT NULL AND q_score<70").fetchone()
            qt = conn.execute("SELECT COUNT(*) c FROM files WHERE state NOT IN ('deleted','removed') "
                              "AND (q_checked IS NULL OR q_checked=0)").fetchone()
            sx = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(size),0) s FROM files "
                              "WHERE state NOT IN ('deleted','removed') AND score>=90 AND q_score IS NOT NULL "
                              "AND q_score<70").fetchone()
            vd = conn.execute("SELECT COALESCE(verdict,'none') g, COUNT(*) c, COALESCE(SUM(size),0) s "
                              "FROM files WHERE state NOT IN ('deleted','removed') GROUP BY g").fetchall()
            av = conn.execute("SELECT AVG(total_score) a FROM files WHERE state NOT IN ('deleted','removed') "
                              "AND total_score IS NOT NULL").fetchone()
            conn.close()
            return self.send_json({"ok": True, "count": tot["c"], "size": tot["s"],
                                   "avg_total": (round(av["a"], 1) if av and av["a"] is not None else None),
                                   "flagged": {"count": fl["c"], "size": fl["s"]},
                                   "quality_bad": {"count": qb["c"], "size": qb["s"]},
                                   "q_todo": qt["c"],
                                   "suspect": {"count": sx["c"], "size": sx["s"]},
                                   "by_verdict": {r["g"]: {"count": r["c"], "size": r["s"]} for r in vd},
                                   "by_flag": {r["g"]: {"count": r["c"], "size": r["s"]} for r in rows}})
        if path == "/api/files":
            return self.list_files(qs)
        if path == "/api/direct":
            p = arg("path") or ""
            if not p or not under_roots(p, STORE.cfg["roots"]):
                return self.err("路径不在已选目录内", 403)
            if not os.path.isfile(p):
                return self.err("文件不存在", 404)
            # 走本应用自己的 Range 直出（飞牛 /download 需要 nginx 签名的 sign，第三方无法生成）
            rel = "api/raw?" + urllib.parse.urlencode({"path": p})
            return self.send_json({"ok": True, "url": rel, "size": os.path.getsize(p), "mode": "range"})
        if path == "/api/raw":
            return self.serve_raw(arg("path") or "")
        if path == "/api/thumb":
            return self.thumb(arg("path"), arg("t") or "10")
        if path == "/api/preview":
            return self.preview(arg("path"), arg("mode") or "remux", arg("start") or "0")
        if path == "/api/audit":
            conn = STORE.db()
            rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 500").fetchall()
            conn.close()
            return self.send_json({"ok": True, "rows": [dict(r) for r in rows]})
        if path == "/api/export.csv":
            return self.export_csv()
        return self.err("unknown api", 404)

    def list_files(self, qs):
        arg = lambda k, d=None: (qs.get(k) or [d])[0]
        page = max(1, int(arg("page") or 1))
        size = min(500, int(arg("size") or 100))
        where, params = ["state NOT IN ('deleted','removed')"], []
        fl = arg("flag")
        if fl and fl != "all":
            if fl == "none":
                where.append("(flags='' OR flags IS NULL)")
            else:
                where.append("flags LIKE ?")
                params.append("%" + fl + "%")
        kw = arg("q")
        if kw:
            where.append("name LIKE ?")
            params.append("%" + kw + "%")
        d = arg("dir")
        if d:
            where.append("dir = ?")
            params.append(d)
        dp = arg("dirprefix")
        if dp:
            dp = dp.rstrip("/")
            where.append("(dir = ? OR dir LIKE ?)")
            params.extend([dp, dp + "/%"])
        qf = arg("qfilter")
        if qf and qf != "all":
            if qf == "todo":
                where.append("(q_checked IS NULL OR q_checked=0)")
            elif qf == "suspect":
                where.append("score>=90 AND q_score IS NOT NULL AND q_score<70")
            elif qf == "fail":
                where.append("q_score IS NOT NULL AND q_score<70")
            elif qf in ("n", "k", "f", "s"):
                where.append("q_flags LIKE ?")
                params.append("%" + qf + "%")
        vf = arg("verdict")
        if vf == "bad":
            where.append("verdict IN ('bad','clean','replace')")
        elif vf in ("good", "ok"):
            where.append("verdict IN ('ok','keep','good')")
        elif vf and vf != "all":
            where.append("verdict = ?")
            params.append(vf)
        sort = arg("sort") or "total"
        sort_sql = {"total": "COALESCE(total_score, score) ASC, size DESC",
                    "total_desc": "COALESCE(total_score, score) DESC, size ASC",
                    "score": "score ASC, size DESC", "size": "size DESC", "name": "name ASC",
                    "bitrate": "vbitrate ASC", "q_score": "q_score ASC, score ASC",
                    "vbitrate": "vbitrate ASC", "res": "res_score ASC, size DESC",
                    "br": "br_score ASC, size DESC",
                    "clarity": "clarity_score ASC, size DESC"}.get(sort, "COALESCE(total_score, score) ASC")
        w = " AND ".join(where)
        conn = STORE.db()
        total = conn.execute("SELECT COUNT(*) c FROM files WHERE " + w, params).fetchone()["c"]
        rows = conn.execute("SELECT id,path,name,dir,size,width,height,fps,bitrate,abitrate,vbitrate,"
                            "vcodec,pix_fmt,acodec,channels,duration,container,score,flags,reason,"
                            "grp,state,q_score,q_flags,q_detail,q_checked,total_score,verdict,"
                            "res_score,br_score,clarity_score "
                            "FROM files WHERE " + w + " ORDER BY " + sort_sql +
                            " LIMIT ? OFFSET ?", params + [size, (page - 1) * size]).fetchall()
        conn.close()
        return self.send_json({"ok": True, "total": total, "page": page, "size": size,
                               "rows": [dict(r) for r in rows]})

    RAW_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".mov": "video/quicktime",
                 ".webm": "video/webm", ".mkv": "video/x-matroska", ".ts": "video/mp2t",
                 ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac"}

    def serve_raw(self, p):
        """原文件直出，支持 HTTP Range（206），浏览器可随意拖动进度条。"""
        if not p or not under_roots(p, STORE.cfg["roots"]):
            return self.err("路径不在已选目录内", 403)
        if not os.path.isfile(p):
            return self.err("文件不存在", 404)
        size = os.path.getsize(p)
        ext = os.path.splitext(p)[1].lower()
        ctype = self.RAW_TYPES.get(ext, "application/octet-stream")
        global RAW_LOG_COUNT
        if RAW_LOG_COUNT < 30:
            RAW_LOG_COUNT += 1
            print("raw req: ext=%s range=%r head=%s ua=%r"
                  % (ext, self.headers.get("Range"), getattr(self, "head_only", False),
                     (self.headers.get("User-Agent") or "")[:60]), flush=True)
        start, end, partial = 0, size - 1, False
        rng = self.headers.get("Range") or ""
        if rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            a, _, b = spec.partition("-")
            try:
                if a == "":
                    n = int(b)
                    start = max(0, size - n)
                    end = size - 1
                else:
                    start = max(0, int(a))
                    end = int(b) if b else size - 1
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                end = min(end, size - 1)
                partial = True
            except Exception:
                start, end, partial = 0, size - 1, False
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if getattr(self, "head_only", False):
            return
        left = length
        try:
            with open(p, "rb") as f:
                f.seek(start)
                while left > 0:
                    chunk = f.read(min(262144, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def thumb(self, path, t):
        if not path or not under_roots(path, STORE.cfg["roots"]):
            return self.err("路径不在已选目录内", 403)
        key = hashlib.md5((path + "|" + t).encode("utf-8")).hexdigest()
        out = os.path.join(STORE.data_dir, "thumbs", key + ".jpg")
        if not os.path.exists(out):
            cmd = [FFMPEG, "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1",
                   "-vf", "scale=480:-2", "-y", out]
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
            except Exception as e:
                return self.err("截图失败: %s" % e, 500)
        if not os.path.exists(out):
            return self.err("截图失败", 500)
        with open(out, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def preview(self, path, mode, start):
        if not path or not under_roots(path, STORE.cfg["roots"]):
            return self.err("路径不在已选目录内", 403)
        if not os.path.isfile(path):
            return self.err("文件不存在", 404)
        if mode == "transcode":
            # 必须显式转 8bit + 缩放：10bit 源直接喂 h264_nvenc 会 "No capable devices found"
            enc = hw_encoder()
            if enc == "h264_nvenc":
                pargs = ["-preset", "p4", "-b:v", "3M", "-maxrate", "4M", "-bufsize", "8M"]
            else:
                pargs = ["-preset", "veryfast", "-crf", "23"]
            vargs = ["-vf", "scale=-2:720", "-pix_fmt", "yuv420p", "-c:v", enc] + pargs
        else:
            vargs = ["-c:v", "copy"]
        # 音轨策略：浏览器能直接放的（aac/mp3/opus）直通，其余转 AAC 立体声；
        # 若转音频失败（TrueHD/DTS-HD 等偶发），自动退回"纯画面"，保证画面能放出来
        acodec = ""
        try:
            conn = STORE.db()
            row = conn.execute("SELECT acodec FROM files WHERE path=?", (path,)).fetchone()
            conn.close()
            acodec = (row["acodec"] or "").lower() if row else ""
        except Exception:
            acodec = ""
        if acodec in ("aac", "mp3", "opus", "vorbis"):
            aargs = ["-c:a", "copy"]
        else:
            aargs = ["-c:a", "aac", "-b:a", "160k", "-ac", "2"]
        def _cmd(with_audio):
            a = (["-map", "0:a:0?"] + aargs) if with_audio else ["-an"]
            return ([FFMPEG, "-v", "error", "-readrate", "4", "-ss", str(start), "-i", path,
                     "-map", "0:v:0"] + a + vargs +
                    ["-movflags", "frag_keyframe+empty_moov+default_base_moof",
                     "-f", "mp4", "pipe:1"])

        proc = subprocess.Popen(_cmd(True), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        first = proc.stdout.read(65536)
        if not first:
            # 音频链路失败（例如 TrueHD 无法转 AAC）→ 重试一次，只要画面
            try:
                proc.kill()
            except Exception:
                pass
            proc = subprocess.Popen(_cmd(False), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            first = proc.stdout.read(65536)
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            if first:
                self.wfile.write(first)
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                proc.kill()
            except Exception:
                pass
            self.close_connection = True

    def export_csv(self):
        conn = STORE.db()
        rows = conn.execute("SELECT path,name,width,height,bitrate,abitrate,vbitrate,vcodec,pix_fmt,"
                            "acodec,duration,size,score,flags,reason,q_score,q_flags,q_detail,"
                            "total_score,verdict,state FROM files WHERE state NOT IN ('deleted','removed')").fetchall()
        conn.close()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["path", "name", "width", "height", "bitrate", "abitrate", "vbitrate", "vcodec",
                    "pix_fmt", "acodec", "duration", "size", "score", "flags", "reason",
                    "q_score", "q_flags", "q_detail", "total_score", "verdict", "state"])
        for r in rows:
            w.writerow([r[k] for k in r.keys()])
        data = buf.getvalue().encode("utf-8-sig")
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", 'attachment; filename="mediaqc.csv"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ---------- POST ----------
    def do_HEAD(self):
        """浏览器探测媒体/静态资源会先发 HEAD，必须返回与 GET 一致的头部。"""
        self.head_only = True
        try:
            return self.do_GET()
        finally:
            self.head_only = False

    def do_POST(self):
        path, qs = self.q()
        self.log_once()
        if not path.startswith("/api/"):
            return self.err("not found", 404)
        if self.need_login():
            return self.deny_login()
        if not self.check_pin(qs):
            return self.err("需要访问 PIN", 401)
        try:
            body = self.body_json()
            if path == "/api/config":
                for k, v in body.items():
                    if k in DEFAULT_CFG and k not in ("port", "roots"):
                        if isinstance(DEFAULT_CFG[k], bool):
                            STORE.cfg[k] = bool(v)
                        elif isinstance(DEFAULT_CFG[k], (int, float)) and not isinstance(DEFAULT_CFG[k], bool):
                            try:
                                STORE.cfg[k] = type(DEFAULT_CFG[k])(v)
                            except Exception:
                                pass
                        else:
                            STORE.cfg[k] = v
                pruned = 0
                if "roots" in body:
                    new_roots = [r.rstrip("/") or "/" for r in body["roots"] if isinstance(r, str) and r]
                    cleaned = []
                    for r in sorted(set(new_roots), key=len):
                        if not any(r == c or r.startswith(c.rstrip("/") + "/") for c in cleaned):
                            cleaned.append(r)
                    new_roots = cleaned
                    STORE.cfg["roots"] = new_roots
                    conn = STORE.db()
                    # 软删除：只标记 removed，保留体检数据（下次把文件夹加回来可复用，不必重检）
                    for r in conn.execute("SELECT id,path,state FROM files WHERE state<>'removed'").fetchall():
                        if not under_roots(r["path"], new_roots):
                            conn.execute("UPDATE files SET state='removed' WHERE id=?", (r["id"],))
                            pruned += 1
                    conn.commit()
                    conn.close()
                STORE.save_cfg()
                print("config update: roots=%r pin_set=%s pruned=%d"
                      % (STORE.cfg["roots"], bool(STORE.cfg.get("pin")), pruned), flush=True)
                return self.send_json({"ok": True, "config": STORE.cfg, "pruned": pruned})
            if path == "/api/inspect":
                with LOCK:
                    if JOB["state"] == "scanning":
                        return self.err("已有任务在运行，请先等待或停止")
                ids = body.get("ids") or None
                force = bool(body.get("force"))
                threading.Thread(target=lambda: self._safe_inspect(ids, force), daemon=True).start()
                return self.send_json({"ok": True})
            if path == "/api/rescore":
                conn = STORE.db()
                rows = conn.execute("SELECT id,name,width,height,fps,bitrate,abitrate,vbitrate,size,"
                                    "duration,vcodec,pix_fmt,q_detail,q_score,flags,ctransfer "
                                    "FROM files WHERE state NOT IN ('deleted','removed')").fetchall()
                n = 0
                nq = 0
                for r in rows:
                    vb = 0
                    dur = float(r["duration"] or 0)
                    if r["size"] and dur > 0:
                        vb = int(max(0, r["size"] * 8 / dur - (r["abitrate"] or 0)))
                    if not vb:
                        vb = r["vbitrate"] or r["bitrate"] or 0
                    rec = {"name": r["name"] or "", "width": r["width"] or 0, "height": r["height"] or 0,
                           "fps": r["fps"] or 0, "bitrate": r["bitrate"] or 0, "vbitrate": vb,
                           "vcodec": r["vcodec"] or "", "pix_fmt": r["pix_fmt"] or ""}
                    sc, flags, why, _bpp = score_of(rec, STORE.cfg)
                    q = r["q_score"]
                    d = None
                    if r["q_detail"]:
                        try:
                            d = json.loads(r["q_detail"]) or {}
                        except Exception:
                            d = None
                    # 只要库里有实测 blur 指标，就用当前阈值重算清晰度
                    # （旧版按版本号跳过，导致老数据一直沿用过期阈值，出现"很清晰却低分"）
                    m = (d or {}).get("metrics") or {}
                    qf = None
                    qw = ""
                    if "bluridx" in m or "blur" in m:
                        q, qf, qw = qc_score(m, STORE.cfg, r["height"] or 0, r["ctransfer"] or "",
                                             r["width"] or 0)
                        nq += 1
                    total, rs, bs, clar, vd, notes = score3(r["width"], r["height"], vb,
                                                            q, flags, STORE.cfg, m.get("block"), qf,
                                                            r["fps"] or 0,
                                                            1 if m.get("_hdr") else 0)
                    if notes:
                        why = (why + "; " if why else "") + "; ".join(notes)
                    if qf is not None:
                        qf = add_upscale_flag(qf, notes)
                        qw2 = "; ".join([x for x in [qw, "; ".join(notes)] if x])
                        conn.execute("UPDATE files SET q_score=?, q_flags=?, q_detail=? WHERE id=?",
                                     (q, qf, json.dumps({"v": QC_VERSION, "metrics": m,
                                                         "why": qw2}, ensure_ascii=False), r["id"]))
                    conn.execute("UPDATE files SET score=?, flags=?, reason=?, vbitrate=?, "
                                 "total_score=?, verdict=?, res_score=?, br_score=?, "
                                 "clarity_score=? WHERE id=?",
                                 (sc, flags, why, vb, total, vd, rs, bs, clar, r["id"]))
                    n += 1
                mark_dups(conn)
                conn.commit()
                conn.close()
                return self.send_json({"ok": True, "rescored": n, "quality_rescored": nq})
            if path == "/api/scan":
                roots = body.get("roots") or STORE.cfg["roots"]
                roots = [r for r in roots if os.path.isdir(r)]
                if not roots:
                    return self.err("请先选择要扫描的目录")
                with LOCK:
                    if JOB["state"] == "scanning":
                        return self.err("已有扫描任务在运行")
                STORE.cfg["roots"] = roots
                STORE.save_cfg()
                threading.Thread(target=lambda: self._safe_scan(roots), daemon=True).start()
                return self.send_json({"ok": True})
            if path == "/api/job/stop":
                JOB_STOP.set()
                return self.send_json({"ok": True})
            if path == "/api/act":
                return self.do_action(body)
            return self.err("unknown api", 404)
        except BrokenPipeError:
            return
        except Exception as e:
            return self.err(str(e), 500)

    def _safe_scan(self, roots):
        try:
            run_scan(roots)
        except Exception as e:
            with LOCK:
                JOB.update(state="idle", msg="扫描出错: %s" % e)
            try:
                conn = STORE.db()
                conn.execute("INSERT INTO audit(ts,action,path,bytes,result,detail) VALUES(?,?,?,?,?,?)",
                             (now(), "scan", ",".join(roots), 0, "error", str(e)))
                conn.commit()
                conn.close()
            except Exception:
                pass

    def _safe_inspect(self, ids, force):
        try:
            run_inspect(ids, force)
        except Exception as e:
            with LOCK:
                JOB.update(state="idle", phase="", msg="画面体检出错: %s" % e)
            print("inspect error:", e, flush=True)

    def do_action(self, body):
        action = body.get("action")
        ids = body.get("ids") or []
        target = body.get("target") or ""
        dry = bool(body.get("dry"))
        if action not in ("keep", "delete", "move"):
            return self.err("未知动作")
        if action == "move":
            if not target:
                return self.err("请先选择移动目标目录")
            if not under_roots(target, STORE.cfg["roots"]):
                return self.err("目标目录必须位于已选扫描目录内", 403)
            if not os.path.isdir(target):
                return self.err("移动目标目录不存在")
        conn = STORE.db()
        plan = []
        for fid in ids:
            row = conn.execute("SELECT id,path,name,size,state FROM files WHERE id=?", (fid,)).fetchone()
            if not row or row["state"] == "deleted":
                continue
            if not under_roots(row["path"], STORE.cfg["roots"]):
                plan.append({"id": fid, "path": row["path"], "error": "不在已选目录内"})
                continue
            plan.append({"id": fid, "path": row["path"], "name": row["name"], "size": row["size"]})
        if dry:
            conn.close()
            return self.send_json({"ok": True, "dry": True, "plan": plan,
                                   "bytes": sum(p.get("size", 0) for p in plan)})
        done = 0
        errors = []
        for p in plan:
            if p.get("error"):
                errors.append(p["error"] + ": " + p["path"])
                continue
            try:
                if action == "delete":
                    size = os.path.getsize(p["path"])
                    os.remove(p["path"])
                    conn.execute("UPDATE files SET state='deleted' WHERE id=?", (p["id"],))
                    conn.execute("INSERT INTO audit(ts,action,path,bytes,result,detail) VALUES(?,?,?,?,?,?)",
                                 (now(), "delete", p["path"], size, "ok", ""))
                elif action == "move":
                    dest = os.path.join(target, p["name"])
                    if os.path.exists(dest):
                        raise RuntimeError("目标已存在同名文件")
                    shutil.move(p["path"], dest)
                    conn.execute("UPDATE files SET path=?, dir=?, state='keep' WHERE id=?",
                                 (dest, target, p["id"]))
                    conn.execute("INSERT INTO audit(ts,action,path,bytes,result,detail) VALUES(?,?,?,?,?,?)",
                                 (now(), "move", p["path"], p["size"], "ok", dest))
                else:
                    conn.execute("UPDATE files SET state='keep' WHERE id=?", (p["id"],))
                    conn.execute("INSERT INTO audit(ts,action,path,bytes,result,detail) VALUES(?,?,?,?,?,?)",
                                 (now(), "keep", p["path"], p["size"], "ok", ""))
                done += 1
            except Exception as e:
                errors.append(str(e) + ": " + p["path"])
                conn.execute("INSERT INTO audit(ts,action,path,bytes,result,detail) VALUES(?,?,?,?,?,?)",
                             (now(), action, p["path"], p.get("size", 0), "error", str(e)))
        conn.commit()
        conn.close()
        return self.send_json({"ok": True, "done": done, "errors": errors})


def main():
    global STORE, PREFIX, REQUIRE_AUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", default="", help="Unix socket 路径（统一网关模式）")
    ap.add_argument("--prefix", default=os.environ.get("FNNAS_GATEWAY_PREFIX", "/app/mediaqc"),
                    help="网关访问前缀，如 /app/mediaqc")
    ap.add_argument("--port", type=int, default=0, help="调试用 TCP 端口（绑定 127.0.0.1）")
    ap.add_argument("--no-auth", action="store_true", help="调试用：跳过网关登录校验")
    ap.add_argument("--data", default=os.path.join(APP_DIR, "..", "data"))
    ap.add_argument("--conf", default=os.path.join(APP_DIR, "..", "etc"))
    args = ap.parse_args()
    STORE = Store(os.path.abspath(args.data), os.path.abspath(args.conf))
    PREFIX = (args.prefix or "").rstrip("/")
    REQUIRE_AUTH = not args.no_auth
    if args.socket:
        srv = UnixHTTPServer(args.socket, Handler)
        print("媒体画质管家 listening on unix:%s prefix=%s auth=%s www=%s"
              % (args.socket, PREFIX, REQUIRE_AUTH, WWW_DIR), flush=True)
    else:
        port = args.port or int(STORE.cfg.get("port") or 8123)
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        print("媒体画质管家 listening on 127.0.0.1:%d prefix=%s auth=%s www=%s"
              % (port, PREFIX, REQUIRE_AUTH, WWW_DIR), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
