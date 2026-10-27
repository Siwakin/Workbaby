# -*- coding: utf-8 -*-
"""wb_crypto.py —— 账号快照的加密导出 / 导入（纯标准库）

用途：把 `登录态快照\\` 里若干账号的整份快照打成一个**口令加密**的单文件，
换机、备份、归档都用它。文件后缀建议 .wbacc（实质是 WBSEAL 容器）。

容器格式（WBSEAL v1）
---------------------
    "WBSEAL1\\n"                  9 字节魔数
    <header_len:4 字节大端>        header 的 UTF-8 JSON 长度
    <header JSON>                 kdf 参数、salt、nonce、导出信息、账号清单
    <ciphertext>                  与明文等长的密文
    <tag:32 字节>                 HMAC-SHA256(mac_key, 魔数‖header_len‖header‖密文)

密钥派生：PBKDF2-HMAC-SHA256(password, salt, 300000 轮) → 64 字节
         前 32 字节做流密钥，后 32 字节做认证密钥。

为什么不用 AES-256-GCM：标准库没有 AES。这里用 HMAC-SHA256 计数器模式做
密钥流（NIST SP800-90A 里 CTR_DRBG 的同一构造）+ HMAC-SHA256 做先加密后
认证（encrypt-then-MAC），安全性上没问题，而且全部走 C 实现的 hashlib/hmac，
3MB 快照只要零点几秒。密码错或文件被改动，认证环节就会失败并明确报错。
"""
import base64
import hashlib
import hmac
import io
import json
import os
import platform
import socket
import struct
import time
import zipfile

MAGIC = b"WBSEAL1\n"
KDF_ITER = 300000
TAG_LEN = 32
SALT_LEN = 16
NONCE_LEN = 16


class SealError(Exception):
    pass


def _derive(password, salt, iters):
    if not isinstance(password, str) or not password:
        raise SealError("密码不能为空")
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iters, 64)
    return dk[:32], dk[32:]


def _keystream(key, nonce, length):
    """HMAC-SHA256 计数器模式密钥流。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out += hmac.new(key, nonce + struct.pack(">Q", counter), hashlib.sha256).digest()
        counter += 1
    return bytes(out[:length])


def _xor(data, ks):
    n = len(data)
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks, "big")).to_bytes(n, "big") if n else b""


def seal(data, password, extra=None, iters=KDF_ITER):
    """把明文 bytes 封成 WBSEAL 容器 bytes。"""
    if not isinstance(data, (bytes, bytearray)):
        raise SealError("seal 需要 bytes")
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    enc_key, mac_key = _derive(password, salt, iters)
    header = {
        "v": 1,
        "kdf": "pbkdf2-hmac-sha256",
        "iter": iters,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "cipher": "hmac-sha256-ctr + hmac-sha256-tag",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": socket.gethostname(),
        "platform": platform.platform(),
        "extra": extra or {},
    }
    hb = json.dumps(header, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ks = _keystream(enc_key, nonce, len(data))
    ct = _xor(bytes(data), ks)
    mac = hmac.new(mac_key, MAGIC + struct.pack(">I", len(hb)) + hb + ct, hashlib.sha256).digest()
    return MAGIC + struct.pack(">I", len(hb)) + hb + ct + mac


def unseal(blob, password):
    """解开 WBSEAL 容器 → (明文 bytes, header dict)。密码错/被篡改抛 SealError。"""
    blob = bytes(blob)
    if len(blob) < len(MAGIC) + 4 + TAG_LEN:
        raise SealError("文件太短，不是本工具的导出文件")
    if not blob.startswith(MAGIC):
        raise SealError("文件头不对，不是本工具的导出文件（或版本不兼容）")
    p = len(MAGIC)
    (hlen,) = struct.unpack(">I", blob[p:p + 4])
    p += 4
    if hlen <= 0 or hlen > 1024 * 1024 or p + hlen + TAG_LEN > len(blob):
        raise SealError("文件结构损坏")
    hb = blob[p:p + hlen]
    p += hlen
    ct = blob[p:len(blob) - TAG_LEN]
    tag = blob[len(blob) - TAG_LEN:]
    try:
        header = json.loads(hb.decode("utf-8"))
        salt = base64.b64decode(header["salt"])
        nonce = base64.b64decode(header["nonce"])
        iters = int(header.get("iter") or KDF_ITER)
    except Exception:
        raise SealError("文件头解析失败")
    enc_key, mac_key = _derive(password, salt, iters)
    calc = hmac.new(mac_key, MAGIC + struct.pack(">I", hlen) + hb + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(calc, tag):
        raise SealError("密码不对，或文件已损坏（认证校验失败）")
    ks = _keystream(enc_key, nonce, len(ct))
    return _xor(ct, ks), header


# ---------------------------------------------------------------- 快照打包

_SAFE_SUB = {"auth", "chromium_local_storage", "chromium_session_storage", "local_storage"}


def _iter_account_files(root, uid):
    """列出一个账号快照目录里应该导出的文件（相对路径）。"""
    base = os.path.join(root, uid)
    if not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base):
        rel = os.path.relpath(dirpath, base)
        for fn in filenames:
            if fn.startswith("_回装前"):
                continue
            rp = fn if rel == "." else os.path.join(rel, fn)
            yield os.path.join(dirpath, fn), rp.replace("\\", "/")


def export_snapshots(root, uids, out_path, password, nicks=None,
                     include_backups=False, progress=None):
    """把指定账号的快照目录打成加密文件。返回摘要 dict。"""
    uids = [u for u in uids if os.path.isdir(os.path.join(root, u))]
    if not uids:
        raise SealError("没有可导出的账号快照")
    nicks = nicks or {}
    buf = io.BytesIO()
    manifest = {"tool": "账号互通工具", "format": "wbseal-v1",
                "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "accounts": []}
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for uid in uids:
            n = 0
            size = 0
            for abspath, rel in _iter_account_files(root, uid):
                if not include_backups and "_回装前" in rel:
                    continue
                arc = "%s/%s" % (uid, rel)
                try:
                    z.write(abspath, arc)
                except Exception:
                    continue
                n += 1
                try:
                    size += os.path.getsize(abspath)
                except Exception:
                    pass
            total += size
            manifest["accounts"].append({"uid": uid, "nick": nicks.get(uid, ""),
                                         "files": n, "bytes": size})
            if progress:
                try:
                    progress(uid, n, size)
                except Exception:
                    pass
        z.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=1))
    raw = buf.getvalue()
    # 注意：头部是明文（只放计数，不放账号 uid/昵称）——账号清单在密文里
    blob = seal(raw, password, extra={"accountCount": len(manifest["accounts"]),
                                      "rawBytes": len(raw), "plainBytes": total})
    tmp = out_path + ".tmp"
    d = os.path.dirname(out_path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    with open(tmp, "wb") as f:
        f.write(blob)
    os.replace(tmp, out_path)
    return {"path": out_path, "bytes": len(blob), "accounts": manifest["accounts"],
            "plain_bytes": total}


def peek(path, password):
    """只解开认证并读 manifest（供导入前预览）。"""
    with open(path, "rb") as f:
        blob = f.read()
    raw, header = unseal(blob, password)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        try:
            man = json.loads(z.read("_manifest.json").decode("utf-8"))
        except Exception:
            man = {"accounts": header.get("extra", {}).get("accounts", [])}
    return man, header


def import_snapshots(path, password, root, overwrite=False, only_uids=None):
    """把加密文件里的账号快照写回 登录态快照\\ 目录。

    只接受 `<uid>/<安全子路径>` 形式，防目录穿越。返回每个账号的结果列表。
    """
    import re
    with open(path, "rb") as f:
        blob = f.read()
    raw, header = unseal(blob, password)
    uid_re = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    results = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = z.namelist()
        # 按 uid 分组
        groups = {}
        for nm in names:
            nm2 = nm.replace("\\", "/")
            parts = nm2.split("/")
            if len(parts) < 2:
                continue
            uid = parts[0]
            if not uid_re.match(uid):
                continue
            if only_uids and uid not in only_uids:
                continue
            groups.setdefault(uid, []).append(nm2)
        for uid, items in groups.items():
            dst_dir = os.path.join(root, uid)
            existed = os.path.isdir(dst_dir)
            if existed and not overwrite:
                results.append({"uid": uid, "action": "skip", "files": 0,
                                "reason": "已存在同名快照（未覆盖）"})
                continue
            written = 0
            for nm in items:
                parts = nm.split("/")
                rel = parts[1:]
                if not rel:
                    continue
                if rel[0].startswith(".") or rel[0].endswith(".exe"):
                    continue
                if any(p in ("", ".", "..") for p in rel):
                    continue
                target = os.path.join(dst_dir, *rel)
                if not os.path.abspath(target).startswith(os.path.abspath(dst_dir) + os.sep):
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                try:
                    with z.open(nm) as src, open(target, "wb") as out:
                        while True:
                            chunk = src.read(262144)
                            if not chunk:
                                break
                            out.write(chunk)
                    written += 1
                except Exception:
                    continue
            results.append({"uid": uid, "action": "overwrite" if existed else "new",
                            "files": written})
    return results, header
