#!/usr/bin/env python3
"""Import a 10GB payload of 500,000 mixed files into the current branch.

The tree is 10,000,000,000 bytes:

  200000 source files  x  8000 bytes  (py, js, ts, go, java, ...)
  150000 text files    x  8000 bytes  (md, json, csv, html, sql, ...)
  100000 images        x 40000 bytes  (png, jpg, gif)
   50000 archives      x 64000 bytes  (zip, tar.gz, tgz, gz)

Files are written straight into packfiles and are not checked out.
Re-running continues from the payload files already in HEAD.
`python3 scripts/generate_scale_repo.py --check` validates one file of each type.
"""
from __future__ import print_function

import base64
import os
import struct
import subprocess
import sys
import time
import zlib

TOTAL_FILES = 500000
TOTAL_BYTES = 10000000000
SRC_COUNT, SRC_SIZE = 200000, 8000
TEXT_COUNT, TEXT_SIZE = 150000, 8000
IMAGE_COUNT, IMAGE_SIZE = 100000, 40000
ARCHIVE_COUNT, ARCHIVE_SIZE = 50000, 64000
FILES_PER_COMMIT = 50000

SRC_EXTS = [
    "py", "js", "ts", "go", "java", "c", "h", "cpp", "rs", "rb",
    "sh", "php", "cs", "kt", "swift", "scala", "lua", "pl", "r", "hs",
]
TEXT_EXTS = [
    "txt", "md", "csv", "json", "yaml", "xml", "html", "css", "sql",
    "log", "ini", "toml", "rst",
]
IMAGE_EXTS = ["png", "jpg", "gif"]
ARCHIVE_EXTS = ["zip", "tar.gz", "tgz", "gz"]

# 1x1 baseline JPEG, SOI stripped off. A COM segment is inserted after SOI.
JPEG_AFTER_SOI = bytes.fromhex(
    "ffe000104a46494600010100000100010000"
    "ffdb004300080606070605080707070909080a0c140d0c0b0b0c1912130f141d1a"
    "1f1e1d1a1c1c20242e2720222c231c1c2837292c30313434341f27393d38323c2e"
    "333432"
    "ffc0000b080001000101011100"
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0b"
    "ffda0008010100003f00fb"
    "ffd9"
)
# 1x1 GIF without the trailing 0x3b. A comment extension is inserted before it.
GIF_PREFIX = bytes.fromhex(
    "47494638396101000100800000ffffff000000"
    "21f9040000000000"
    "2c000000000100010000"
    "0202440100"
)

WRAPPERS = {
    "py": ("# fixture {i}\n\ndef run_{i}():\n    return '''", "'''\n"),
    "js": ("// fixture {i}\nfunction run_{i}() {{\n  return \"", "\";\n}}\n"),
    "ts": ("// fixture {i}\nexport function run_{i}(): string {{\n  return \"", "\";\n}}\n"),
    "go": ("package fixture\n\n// file {i}\nfunc Run{i}() string {{\n\treturn \"", "\"\n}}\n"),
    "java": ("public class Fixture{i} {{\n  public String run() {{\n    return \"", "\";\n  }}\n}}\n"),
    "c": ("/* fixture {i} */\nconst char *run_{i}(void) {{ return \"", "\"; }}\n"),
    "h": ("#ifndef FIXTURE_{i}_H\n#define FIXTURE_{i}_H\nstatic const char FIXTURE_{i}[] = \"", "\";\n#endif\n"),
    "cpp": ("// fixture {i}\ninline const char *run_{i}() {{ return \"", "\"; }}\n"),
    "rs": ("// fixture {i}\npub fn run_{i}() -> &'static str {{\n    \"", "\"\n}}\n"),
    "rb": ("# fixture {i}\ndef run_{i}\n  \"", "\"\nend\n"),
    "sh": ("#!/bin/sh\n# fixture {i}\nrun_{i}() {{\n  printf '%s' \"", "\"\n}}\n"),
    "php": ("<?php\n// fixture {i}\nfunction run_{i}() {{\n  return \"", "\";\n}}\n"),
    "cs": ("public class Fixture{i} {{\n  public string Run() {{\n    return \"", "\";\n  }}\n}}\n"),
    "kt": ("fun run_{i}(): String {{\n  return \"", "\"\n}}\n"),
    "swift": ("func run_{i}() -> String {{\n  return \"", "\"\n}}\n"),
    "scala": ("object Fixture{i} {{\n  def run(): String = \"", "\"\n}}\n"),
    "lua": ("-- fixture {i}\nlocal function run_{i}()\n  return \"", "\"\nend\n"),
    "pl": ("# fixture {i}\nsub run_{i} {{\n  return \"", "\";\n}}\n"),
    "r": ("# fixture {i}\nrun_{i} <- function() {{\n  \"", "\"\n}}\n"),
    "hs": ("-- fixture {i}\nrun_{i} = \"", "\"\n"),
    "txt": ("fixture {i}\n", "\n"),
    "md": ("# Fixture {i}\n\n", "\n"),
    "csv": ("id,value\n{i},", "\n"),
    "json": ('{{"id":{i},"body":"', '"}}\n'),
    "yaml": ('id: {i}\nbody: "', '"\n'),
    "xml": ('<?xml version="1.0"?>\n<item id="{i}"><![CDATA[', "]]></item>\n"),
    "html": ("<!DOCTYPE html><html><title>{i}</title><pre>", "</pre></html>\n"),
    "css": ('/* fixture {i} */\n.f{i}::after {{ content: "', '"; }}\n'),
    "sql": ("-- fixture {i}\nINSERT INTO fixture(id, body) VALUES ({i}, '", "');\n"),
    "log": ("INFO fixture {i} ", "\n"),
    "ini": ("[fixture]\nid={i}\nbody=", "\n"),
    "toml": ('id = {i}\nbody = "', '"\n'),
    "rst": ("Fixture {i}\n\n", "\n"),
}

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def random_ascii(n):
    out = bytearray()
    while len(out) < n:
        need = n - len(out)
        raw = os.urandom((need * 3) // 4 + 3)
        out += base64.b64encode(raw)
    return bytes(out[:n])


def fit_text(header, footer, size):
    head = header.encode()
    foot = footer.encode()
    need = size - len(head) - len(foot)
    if need < 0:
        raise ValueError("wrapper longer than %d" % size)
    return head + random_ascii(need) + foot


def png_chunk(tag, data):
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def png_bytes(size):
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = png_chunk(b"IDAT", zlib.compress(b"\x00\x00\x00\x00"))
    iend = png_chunk(b"IEND", b"")
    keyword = b"Comment\x00"
    text_len = size - (len(sig) + len(ihdr) + len(idat) + len(iend) + 12 + len(keyword))
    if text_len < 0:
        raise ValueError("png target %d too small" % size)
    text = png_chunk(b"tEXt", keyword + random_ascii(text_len))
    blob = sig + ihdr + idat + text + iend
    if len(blob) != size:
        raise ValueError("png %d != %d" % (len(blob), size))
    return blob


def jpg_bytes(size):
    data_len = size - (2 + 4 + len(JPEG_AFTER_SOI))
    if data_len < 0 or data_len > 65533:
        raise ValueError("jpg data_len %d" % data_len)
    comment = b"\xff\xfe" + struct.pack(">H", data_len + 2) + random_ascii(data_len)
    blob = b"\xff\xd8" + comment + JPEG_AFTER_SOI
    if len(blob) != size or not blob.endswith(b"\xff\xd9"):
        raise ValueError("jpg %d != %d" % (len(blob), size))
    return blob


def gif_comment(data):
    out = bytearray(b"\x21\xfe")
    i = 0
    n = len(data)
    while i < n:
        take = min(255, n - i)
        out.append(take)
        out += data[i:i + take]
        i += take
    out.append(0)
    return bytes(out)


def gif_bytes(size):
    trailer = b"\x3b"
    room = size - len(GIF_PREFIX) - len(trailer)

    def encoded(n):
        subs = 0 if n <= 0 else (n + 254) // 255
        return 2 + n + subs + 1

    n = max(0, room - 3 - room // 256)
    for _ in range(8):
        got = encoded(n)
        if got == room:
            break
        n += room - got
    if encoded(n) != room:
        raise ValueError("gif cannot fill %d (room %d)" % (size, room))
    blob = GIF_PREFIX + gif_comment(random_ascii(n)) + trailer
    if len(blob) != size:
        raise ValueError("gif %d != %d" % (len(blob), size))
    return blob


def zip_bytes(size, name=b"entry.txt"):
    data_len = size - (98 + 2 * len(name))
    if data_len < 0:
        raise ValueError("zip target %d too small" % size)
    data = os.urandom(data_len)
    crc = zlib.crc32(data) & 0xFFFFFFFF
    local = struct.pack(
        "<IHHHHHIIIHH",
        0x04034B50, 20, 0, 0, 0, 0, crc, data_len, data_len, len(name), 0,
    ) + name + data
    central = struct.pack(
        "<IHHHHHHIIIHHHHHII",
        0x02014B50, 20, 20, 0, 0, 0, 0, crc, data_len, data_len,
        len(name), 0, 0, 0, 0, 0, 0,
    ) + name
    end = struct.pack(
        "<IHHHHIIH",
        0x06054B50, 0, 0, 1, 1, len(central), len(local), 0,
    )
    blob = local + central + end
    if len(blob) != size:
        raise ValueError("zip %d != %d" % (len(blob), size))
    return blob


def gzip_stored(payload, comment=b""):
    flag = 0x10 if comment else 0x00
    # ID1 ID2 CM FLG MTIME(4) XFL OS
    header = struct.pack("<4BI2B", 0x1F, 0x8B, 8, flag, 0, 0, 0xFF)
    if comment:
        if b"\x00" in comment:
            raise ValueError("gzip comment contains NUL")
        header += comment + b"\x00"
    block = b"\x01" + struct.pack("<HH", len(payload), len(payload) ^ 0xFFFF) + payload
    trailer = struct.pack("<II", zlib.crc32(payload) & 0xFFFFFFFF, len(payload) & 0xFFFFFFFF)
    return header + block + trailer


def gz_bytes(size):
    data_len = size - 23
    if not 0 <= data_len <= 65535:
        raise ValueError("gz data_len %d" % data_len)
    blob = gzip_stored(os.urandom(data_len))
    if len(blob) != size:
        raise ValueError("gz %d != %d" % (len(blob), size))
    return blob


def tar_header(name, size):
    hdr = bytearray(512)
    nb = name.encode()[:100]
    hdr[0:len(nb)] = nb
    hdr[100:108] = b"0000644\0"
    hdr[108:116] = b"0000000\0"
    hdr[116:124] = b"0000000\0"
    hdr[124:136] = ("%011o\0" % size).encode()
    hdr[136:148] = b"00000000000\0"
    hdr[148:156] = b"        "
    hdr[156] = ord("0")
    hdr[257:263] = b"ustar\0"
    hdr[263:265] = b"00"
    chk = sum(hdr) & 0o777777
    hdr[148:156] = ("%06o\0 " % chk).encode()
    return bytes(hdr)


def targz_bytes(size):
    # One stored deflate block can hold the tar, and a comment pads to `size`.
    member = 121 * 512
    tar = tar_header("entry.txt", member) + os.urandom(member) + (b"\0" * 1024)
    if len(tar) > 65535:
        raise ValueError("tar too large for one stored block")
    comment_len = size - (24 + len(tar))
    if comment_len < 0:
        raise ValueError("targz comment %d" % comment_len)
    blob = gzip_stored(tar, random_ascii(comment_len))
    if len(blob) != size:
        raise ValueError("targz %d != %d" % (len(blob), size))
    return blob


def meta(index):
    if index < SRC_COUNT:
        return "src", SRC_EXTS[index % len(SRC_EXTS)], SRC_SIZE
    index -= SRC_COUNT
    if index < TEXT_COUNT:
        return "text", TEXT_EXTS[index % len(TEXT_EXTS)], TEXT_SIZE
    index -= TEXT_COUNT
    if index < IMAGE_COUNT:
        return "image", IMAGE_EXTS[index % len(IMAGE_EXTS)], IMAGE_SIZE
    index -= IMAGE_COUNT
    if index < ARCHIVE_COUNT:
        return "archive", ARCHIVE_EXTS[index % len(ARCHIVE_EXTS)], ARCHIVE_SIZE
    raise IndexError(index)


def path_for(index):
    kind, ext, _size = meta(index)
    return "payload/%s/%03d/%06d.%s" % (kind, index // 1000, index, ext)


def content_for(index):
    _kind, ext, size = meta(index)
    if ext in ("png",):
        blob = png_bytes(size)
    elif ext == "jpg":
        blob = jpg_bytes(size)
    elif ext == "gif":
        blob = gif_bytes(size)
    elif ext == "zip":
        blob = zip_bytes(size)
    elif ext in ("tar.gz", "tgz"):
        blob = targz_bytes(size)
    elif ext == "gz":
        blob = gz_bytes(size)
    else:
        header, footer = WRAPPERS[ext]
        blob = fit_text(header.format(i=index), footer.format(i=index), size)
    if len(blob) != size:
        raise ValueError("%s is %d bytes" % (path_for(index), len(blob)))
    return blob


def self_check():
    import gzip
    import io
    import tarfile
    import zipfile

    expected = (
        SRC_COUNT * SRC_SIZE
        + TEXT_COUNT * TEXT_SIZE
        + IMAGE_COUNT * IMAGE_SIZE
        + ARCHIVE_COUNT * ARCHIVE_SIZE
    )
    counts = SRC_COUNT + TEXT_COUNT + IMAGE_COUNT + ARCHIVE_COUNT
    if expected != TOTAL_BYTES or counts != TOTAL_FILES:
        raise SystemExit("scale constants do not add up: %s files %s bytes" % (counts, expected))

    groups = [
        (0, SRC_EXTS),
        (SRC_COUNT, TEXT_EXTS),
        (SRC_COUNT + TEXT_COUNT, IMAGE_EXTS),
        (SRC_COUNT + TEXT_COUNT + IMAGE_COUNT, ARCHIVE_EXTS),
    ]
    for base, exts in groups:
        for offset, ext in enumerate(exts):
            index = base + offset
            blob = content_for(index)
            path = path_for(index)
            if not path.endswith("." + ext):
                raise SystemExit("path %s does not end with %s" % (path, ext))
            if ext == "png" and not blob.startswith(b"\x89PNG\r\n\x1a\n"):
                raise SystemExit("bad png")
            if ext == "jpg" and not (blob.startswith(b"\xff\xd8") and blob.endswith(b"\xff\xd9")):
                raise SystemExit("bad jpg")
            if ext == "gif" and not (blob.startswith(b"GIF89a") and blob.endswith(b"\x3b")):
                raise SystemExit("bad gif")
            if ext == "zip":
                with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                    if zf.testzip() is not None or zf.read("entry.txt") == b"":
                        raise SystemExit("bad zip")
            if ext == "gz":
                if not gzip.decompress(blob):
                    raise SystemExit("bad gz")
            if ext in ("tar.gz", "tgz"):
                with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
                    member = tf.getmember("entry.txt")
                    if member.size <= 0 or tf.extractfile(member).read(1) == b"":
                        raise SystemExit("bad tar")
            print("ok %s %d" % (path, len(blob)))
    print("CHECK files=%d bytes=%d" % (TOTAL_FILES, TOTAL_BYTES))


def git_bytes(args):
    return subprocess.check_output(["git"] + args, cwd=ROOT)


def free_bytes():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


def payload_state():
    proc = subprocess.Popen(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "payload"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    count = 0
    last = ""
    for raw in proc.stdout:
        count += 1
        last = raw.decode().rstrip("\n")
    if proc.wait() != 0:
        raise SystemExit("git ls-tree failed")
    return count, last


def sync_index():
    subprocess.check_call(["git", "reset", "-q", "--mixed", "HEAD"], cwd=ROOT)
    listed = subprocess.Popen(
        ["git", "ls-files", "-z", "--", "payload"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    updated = subprocess.Popen(
        ["git", "update-index", "--skip-worktree", "-z", "--stdin"],
        cwd=ROOT,
        stdin=listed.stdout,
    )
    listed.stdout.close()
    if listed.wait() != 0 or updated.wait() != 0:
        raise SystemExit("failed to mark payload skip-worktree")


def verify():
    proc = subprocess.Popen(
        ["git", "ls-tree", "-r", "-l", "HEAD", "payload"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
    )
    count = 0
    total = 0
    for raw in proc.stdout:
        total += int(raw.split()[3])
        count += 1
    if proc.wait() != 0:
        raise SystemExit("verify ls-tree failed")
    print("DONE files=%d bytes=%d" % (count, total), flush=True)
    if count != TOTAL_FILES or total != TOTAL_BYTES:
        raise SystemExit("size mismatch: expected %d files and %d bytes" % (TOTAL_FILES, TOTAL_BYTES))


def import_range(start, end, branch, author, committer):
    parent = git_bytes(["rev-parse", "HEAD"]).strip()
    message = ("Add payload files %d-%d\n" % (start, end - 1)).encode()
    payload_bytes = sum(meta(i)[2] for i in range(start, end))
    need = payload_bytes + 8 * 1024 ** 3
    avail = free_bytes()
    if avail < need:
        raise SystemExit("not enough disk: have %d bytes, want %d" % (avail, need))
    log_path = os.path.join(ROOT, ".scale-fast-import.log")
    log = open(log_path, "a")
    log.write("\n--- import %d %d ---\n" % (start, end))
    log.flush()
    proc = subprocess.Popen(
        [
            "git", "-c", "core.compression=0", "-c", "pack.compression=0",
            "fast-import", "--depth=0", "--active-branches=1",
            "--max-pack-size=2g", "--done",
        ],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    stream = proc.stdin
    try:
        stream.write(b"commit " + branch + b"\n")
        stream.write(b"author " + author + b"\n")
        stream.write(b"committer " + committer + b"\n")
        stream.write(b"data %d\n" % len(message))
        stream.write(message)
        stream.write(b"from " + parent + b"\n")
        for index in range(start, end):
            blob = content_for(index)
            stream.write(b"M 100644 inline " + path_for(index).encode() + b"\n")
            stream.write(b"data %d\n" % len(blob))
            stream.write(blob)
            done = index + 1
            if done % 5000 == 0:
                stream.flush()
                print(
                    "PROGRESS files=%d elapsed_s=%d free_gb=%.1f"
                    % (done, int(time.time() - STARTED), free_bytes() / 1024.0 ** 3),
                    flush=True,
                )
        stream.write(b"done\n")
        stream.close()
    except Exception:
        try:
            stream.close()
        except Exception:
            pass
        proc.wait()
        raise
    code = proc.wait()
    log.close()
    if code != 0:
        raise SystemExit("fast-import failed, log %s" % log_path)
    sync_index()


def main():
    global STARTED
    STARTED = time.time()
    os.chdir(ROOT)
    branch_name = git_bytes(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
    if branch_name in (b"HEAD", b"main", b"master"):
        raise SystemExit("refusing to import onto %s" % branch_name.decode())
    count, last = payload_state()
    if count > TOTAL_FILES:
        raise SystemExit("HEAD already has %d payload files" % count)
    if count:
        if last != path_for(count - 1):
            raise SystemExit("unexpected last payload path: %s" % last)
        print("resume files=%d" % count, flush=True)
    if count == TOTAL_FILES:
        sync_index()
        verify()
        return
    author = git_bytes(["var", "GIT_AUTHOR_IDENT"]).strip()
    committer = git_bytes(["var", "GIT_COMMITTER_IDENT"]).strip()
    branch = b"refs/heads/" + branch_name
    start = count
    while start < TOTAL_FILES:
        end = min(start + FILES_PER_COMMIT, TOTAL_FILES)
        print("chunk %d-%d" % (start, end - 1), flush=True)
        import_range(start, end, branch, author, committer)
        start = end
    verify()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        self_check()
    else:
        main()
