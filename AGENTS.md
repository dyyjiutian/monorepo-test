# AGENTS.md

本仓库用来测试 GitHub 上大 monorepo 的行为。这里没有业务代码。

典型实验：造出一个大约 **10GB**、**50 万个文件** 的仓库，文件里混有源代码、文本、图片和压缩包。再用普通的 `git push` 和 `git clone` 看整条路径能否走完。记录成功或失败、耗时，以及 Git / GitHub 返回的原文。

## 实验协议

推送和克隆使用普通 Git：完整历史、默认 checkout。对比项（Git LFS、partial clone、sparse-checkout、拆仓库）只有用户点名要测时才加。

造数时遵守这些约束：

- 用户明确说开始造数据之前，不要生成这批文件。先看磁盘再动手。
- 用 `scripts/generate_scale_repo.py` 生成。合计 500000 个文件、10000000000 字节（10GB）：
  - 20 万个源文件（py/js/ts/go/java/c/cpp/rs 等），每个 8000 字节
  - 15 万个文本（md/json/csv/html/sql 等），每个 8000 字节
  - 10 万个图片（png/jpg/gif），每个 40000 字节
  - 5 万个压缩包（zip/tar.gz/tgz/gz），每个 64000 字节
- 单个文件保持在 GitHub 的推送硬限制以下。超过 100MB 的文件会被拒绝，超过 50MB 会警告。这里最大的文件是 64KB。
- push 或 clone 失败时，留下命令、Git 版本和完整报错。这就是实验结果。
- 不要 force push，不要改 git config，不要提交密钥。
- 体积数据一旦进库，后续不要再造一份叠加上去，也不要在用户确认前删掉已推送的测试数据。

GitHub 公开建议仓库尽量小于 1GB，并强烈建议小于 5GB。本实验故意超出该建议，用来观察平台实际是否接受、以及 clone 是否还能完成。

## 本地磁盘

脚本把对象直接写入 pack，不在工作区展开 `payload/`。不要对 `payload/` 执行 `git restore` 或 `git checkout`，那会再写出约 10GB。

## 仓库现状

- 远端：`git@github.com:dyyjiutian/monorepo-test.git`
- 当前分支：`dyyjiutian/mono`（worktree，相对 `main`）
- 造数脚本：`scripts/generate_scale_repo.py`

## 结果记录

一次实验做完后，在仓库里留一段短记录：数据规模、使用的命令、push 与 clone 是否完成、耗时、失败时的错误。没有新的实验结论时，不要改已有记录。
