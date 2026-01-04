#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
download_avatars.py

遍历 chat_records 目录下所有的 .ndjson 文件，提取群员头像 URL 并下载到本地。
- 优先 from_user.avatar_large，其次 from_user.profile_image_url
- 已存在文件默认跳过；--force 可覆盖
- 统一存放至 static/avatars
"""

import os, sys, json, re, argparse
from pathlib import Path
import requests

def pick_url(u: dict) -> str | None:
    if not isinstance(u, dict):
        return None
    for k in ("avatar_large", "profile_image_url"):
        v = u.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None

def guess_ext(url: str, content_type: str | None) -> str:
    if content_type:
        ct = content_type.lower()
        if "jpeg" in ct or "jpg" in ct: return "jpg"
        if "png" in ct: return "png"
        if "webp" in ct: return "webp"
    m = re.search(r"\.(jpg|jpeg|png|webp)(?:\?|$)", (url or "").lower())
    if not m:
        return "jpg"
    ext = m.group(1)
    return "jpg" if ext == "jpeg" else ext

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", dest="indir", default="chat_records", help="NDJSON 所在目录")
    ap.add_argument("--out", dest="outdir", default="static/avatars", help="输出目录")
    ap.add_argument("--force", action="store_true", help="覆盖已存在文件")
    ap.add_argument("--timeout", type=int, default=15)
    args = ap.parse_args()

    indir = Path(args.indir)
    outdir = Path(args.outdir)
    
    if not indir.exists() or not indir.is_dir():
        sys.exit(f"呜呜，找不到目录: {indir}w")

    outdir.mkdir(parents=True, exist_ok=True)

    # 查找所有 ndjson 文件
    ndjson_files = list(indir.glob("**/*.ndjson"))
    if not ndjson_files:
        print(f"在 {indir} 没找到任何 .ndjson 文件呢w")
        return

    print(f"找到 {len(ndjson_files)} 个 ndjson 文件，正在解析用户信息...")

    sess = requests.Session()
    sess.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://api.weibo.com/",
    })

    # 从所有文件聚合 uid -> url
    uid2url: dict[str, str] = {}
    for ndjson in ndjson_files:
        try:
            with ndjson.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try:
                        msg = json.loads(line)
                    except: continue
                    
                    u = msg.get("from_user") or {}
                    uid = u.get("id") or msg.get("from_uid")
                    if uid is None: continue
                    
                    uid = str(uid)
                    url = pick_url(u)
                    if url:
                        # 如果同一个 UID 在不同文件里有不同 URL，这里会保留最后一次见到的
                        uid2url[uid] = url
        except Exception as e:
            print(f"[WARN] 读取文件 {ndjson} 出错: {e}")

    print(f"解析完成，共有 {len(uid2url)} 个独立用户的头像需要处理w")

    ok = skip = fail = 0
    for uid, url in uid2url.items():
        # 检查是否已存在 (支持多种常见扩展名)
        existed = any((outdir / f"{uid}.{ext}").exists() for ext in ("jpg", "png", "webp"))
        if existed and not args.force:
            skip += 1
            continue

        try:
            r = sess.get(url, timeout=args.timeout)
            if r.status_code == 200 and r.content:
                ext = guess_ext(url, r.headers.get("Content-Type"))
                out = outdir / f"{uid}.{ext}"
                out.write_bytes(r.content)
                ok += 1
                print(f"[OK] {uid} -> {out}")
            else:
                fail += 1
                print(f"[ERR] {uid} http={r.status_code}")
        except Exception as e:
            fail += 1
            print(f"[ERR] {uid} 发生错误: {e}")

    print("-" * 30)
    print(f"任务结束：成功 {ok}，跳过 {skip}，失败 {fail}")
    print(f"头像存放在: {outdir.resolve()}")

if __name__ == "__main__":
    main()