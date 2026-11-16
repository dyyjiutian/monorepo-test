#!/usr/bin/env python3
"""Import a 50GB payload of 500,000 files into the current branch.

Each file is 100,000 random bytes (50,000,000,000 bytes total). Random bytes
do not compress, so the packfiles are about 50GB. The script writes packfiles
only and does not check the files out.

Safe to re-run: it counts payload files already in HEAD and continues.
"""
from __future__ import print_function

import os
import subprocess
import sys
import time

TOTAL_FILES = 500000
FILE_SIZE = 100000
FILES_PER_COMMIT = 50000
PER_DIR = 1000
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def git_bytes(args):
    return subprocess.check_output(["git"] + args, cwd=ROOT)


def free_bytes():
    st = os.statvfs(ROOT)
    return st.f_bavail * st.f_frsize


def path_for(index):
    return "payload/%03d/%06d.bin" % (index // PER_DIR, index)


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
        size = int(raw.split()[3])
        count += 1
        total += size
    if proc.wait() != 0:
        raise SystemExit("verify ls-tree failed")
    print("DONE files=%d bytes=%d" % (count, total), flush=True)
    if count != TOTAL_FILES or total != TOTAL_FILES * FILE_SIZE:
        raise SystemExit(
            "size mismatch: expected %d files and %d bytes"
            % (TOTAL_FILES, TOTAL_FILES * FILE_SIZE)
        )


def import_range(start, end, branch, author, committer):
    parent = git_bytes(["rev-parse", "HEAD"]).strip()
    message = ("Add payload files %d-%d\n" % (start, end - 1)).encode()
    need = (end - start) * FILE_SIZE + 8 * 1024 ** 3
    avail = free_bytes()
    if avail < need:
        raise SystemExit(
            "not enough disk: have %d bytes, want %d bytes before this chunk"
            % (avail, need)
        )
    log_path = os.path.join(ROOT, ".scale-fast-import.log")
    log = open(log_path, "a")
    log.write("\n--- import %d %d ---\n" % (start, end))
    log.flush()
    cmd = [
        "git",
        "-c", "core.compression=0",
        "-c", "pack.compression=0",
        "fast-import",
        "--depth=0",
        "--active-branches=1",
        "--max-pack-size=2g",
        "--done",
    ]
    proc = subprocess.Popen(
        cmd,
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
            stream.write(b"M 100644 inline " + path_for(index).encode() + b"\n")
            stream.write(b"data %d\n" % FILE_SIZE)
            stream.write(os.urandom(FILE_SIZE))
            done = index + 1
            if done % 5000 == 0:
                # progress is not a file command; inside a commit it ends the commit.
                stream.flush()
                print(
                    "PROGRESS files=%d elapsed_s=%d free_gb=%.1f"
                    % (done, int(time.time() - STARTED), free_bytes() / 1024.0 ** 3),
                    flush=True,
                )
        stream.write(b"done\n")
        stream.close()
    except Exception:
        stream.close()
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
    main()
