# 把仓库上传到 GitHub（脱敏 + 推送）

> 触发：用户要求「把完成的这个程序上传到 github 上，确保不会泄露我的个人 api 和隐私数据。
> 打包项目如果太大不方便就不上传」。
> 目标仓库：`git@github.com:Kinen4062/my-project-video.git`（已存在，走 SSH）。

这一轮没改任何播放器功能，只碰了仓库本身。但里面有四个「看着是小事、实际会让隐私漏出去
或者让推送变成事故」的判断，值得记下来。

---

## 一、先摆事实：要上传的到底是什么

| 项 | 实测 |
|---|---|
| 本地 main 领先远程 | **20 个提交** |
| 远程已有 | `16b53ed first commit` + `4c87f1f 更新记录`（＝ 一份 233 行的 README.md） |
| 被跟踪文件 | 70 个 |
| **实际要推送的内容** | **约 13 MB**（`city.mp4` 3.87 MB + 14 张测试截图 + 源码） |
| `.git` 目录 | **124 MB** |

### 坑 1：别被 `size-pack` 骗了

`git count-objects -vH` 报 `size-pack: 115.74 MiB`，看着像要传一百多兆。实际拆开：

```
可达对象   （= 会推送）   最大 blob 3.87 MB（test_videos/city.mp4）
不可达对象 （= 本地垃圾） 329.2 MB
```

那 329 MB 是历史上删掉又没回收的 blob（`git fsck --unreachable` 能列出来）。
**它们不会上传** —— push 只发远程缺失的**可达**对象。实测推出去的 pack 只有 **5.58 MiB**。

判据要用 `git rev-list --objects --all` 配 `git cat-file --batch-check`，
**不要看 `size-pack`**，它含本地垃圾。

### 用户说的「太大就不上传」

打包产物 `dist/final_player_win64.zip`（409 MB）和 `dist/final_player/`（934 MB）
**本来就在 `.gitignore` 里**，而且查过历史 —— 初始提交只有 10 个文件，从来没有
`dist/`。所以「不传大文件」这件事**不需要额外动作**，但**需要验证**（见第二节）。

---

## 二、隐私审计：分三层，缺一层就会漏

| 层 | 做法 | 结果 |
|---|---|---|
| ① 工作区文件 | 逐文件扫描 + `git grep` | 初始干净 |
| ② **每个历史版本** | 对 `git rev-list --all` 的每个提交 `git grep` | ⚠️ 12 处 |
| ③ **提交信息本身** | `git log --format='%B'` | 干净 |

第 ③ 层最容易忘 —— commit message 同样会跟着推送公开出去。

### 实测结论

- **无任何凭据**：全历史搜 `sk-` / `ghp_` / `github_pat_` / `AIza` / 40 位十六进制 → 全空。
- **`online.json`（OpenSubtitles API key）从未入库**：它一直在 `%APPDATA%/final_player/`，
  在项目目录之外；`.gitignore` 末尾还有一层兜底注释写明「key 一旦进了 git 历史，
  删文件是没用的」。
- **打包产物从未入库**（见上）。
- ⚠️ **要清理的只有一件**：`docs/` 下 **10 个文档、12 处**写了本机绝对路径
  （`C:\Users\<用户名>\PycharmProjects\PythonProject1\...` 这种）。含 Windows 用户名与
  目录结构，**不含凭据**。

### 坑 2（本轮最值钱的一条）：MSYS 会把 `git grep` 的参数当路径转换掉

```bash
git grep -c -F 'C:\Users'  -- .    # → 只报 1 个文件
git grep -c -F '<用户名>'  -- .    # → 报 10 个文件 ← 这个才是真的
```

Git Bash（MSYS2）会把**看起来像 Windows 路径的参数转换掉再传给 git**，所以带盘符的
搜索模式会**静默失效**。报得比实际少，而「少报」在隐私审计里是最危险的失败方向 ——
你会以为干净了。

**规矩：审计判据只用不含路径特征的短串（这里是用户名），不要用完整路径。**

同一个坑也会影响 Python 侧：`git ls-files` 对非 ASCII 文件名**默认输出 C 风格转义**
（`"docs/...\344\277\256..."`），直接 `open()` 必然全部 OSError、统计恒为 0。
用 `git ls-files -z` 按字节切，或者 `-c core.quotepath=false`。

---

## 三、先处理分叉，再谈推送

`git fetch` 之后才发现：远程 main 是 `4c87f1f 更新记录`（在网页上加的 README），
而**本地缓存的 `origin/main` 还停在 `16b53ed`**。不 fetch 就会以为能快进，
推的时候才发现被拒。

处理方式是 **rebase，不是 force push**：

```bash
git rebase origin/main        # 21 个提交重放到 README 之上
```

理由：
- README 是远程新增的**独立文件**，与本地改动无冲突；
- rebase 后 `origin/main` 成为 `main` 的**祖先** → **可以快进推送**，
  不需要 force，也就**不会覆盖掉那份 README**。

> 那份 README 是用户自己写的，不能丢。要在 force 之前先回答「远程有什么是我没有的」，
> 这是义务，不是可选项。

---

## 四、脱敏：一份脚本，两处复用

要清的不只是**当前文件**，还有**历史里那些文档的旧版本**（同一个文件在早先的提交里
同样带路径）。如果给两处各写一套替换规则，早晚不一致、历史里漏掉几处。

所以规则只写一份 —— `build/scrub_paths.py`：

- 工作区清理：`python build/scrub_paths.py .`
- 历史重写：`git filter-branch --tree-filter "<python> <脚本> ." origin/main..HEAD`

脚本本身的四个约束（每条都对应一种会出事的写法）：

1. **只碰文本**（后缀白名单 + `decode('utf-8')` 失败就跳过）—— 否则会把 PNG / MP4 读坏。
2. **无变化就不写回** —— 保持字节与 mtime 不变，不制造空 diff。
3. **走「二进制读 → 解码 → 替换 → 编码 → 二进制写」** —— 保证除替换点外**逐字节不变**。
   从文本层面重写整个文件会污染每一处 diff（换行符、BOM、行尾空格都会变）。
4. 替换规则**长匹配排在短匹配前面** —— 否则 `...\PythonProject1\` 会被
   `...\PythonProject1` 先吃掉，留下一个孤零零的反斜杠。

替换后：本机绝对路径 → 相对路径（`final_player.py`）或占位符（`<视频目录>`）。

---

## 五、历史重写：filter-branch + 一个必做的验证

```bash
git filter-branch -f --tree-filter "<python> <脚本> ." origin/main..HEAD
```

- 范围是 `origin/main..HEAD`（21 个提交），**不含**共同的祖先 `16b53ed` ——
  它是远程已有的提交，重写它会导致必须 force push，而它本来就干净（只有 10 个文件）。
- 本机没有 `git filter-repo`（脚本和模块都查过），用 git 自带的 `filter-branch`；
  那条「has a glut of gotchas」的警告可以忽略，但**必须配合验证**。

### 验证方法（关键）：和备份分支做 diff

重写前先 `git branch backup-20261016-pre-upload`。重写后：

```bash
git diff --stat backup-20261016-pre-upload main
```

**期望值：只有那 10 个文件各 2 行（＝12 处替换）+ 远程合并进来的 README.md。**
实测结果 `11 files changed, 245 insertions(+), 12 deletions(-)` —— 完全吻合。

这个 diff 是**最强的完整性判据**：它同时证明「该清的清了」和「没误伤别的文件」
（MP4、截图、源码任何一处被动过都会在这里露出来）。只看 `git status` 是不够的。

另外两条验证：

```bash
# 全历史是否还有残留（应为空）
git rev-list main | while read c; do git grep -l -I -F '<用户名>' $c -- .; done | sort -u

# 是否能快进（避免 force）
git merge-base --is-ancestor origin/main main && echo 可以
```

---

## 六、上传了什么 / 没上传什么

**上传**：70 个被跟踪文件 —— 源码 8 个模块、`tests/` 13 个套件、`docs/` 全部过程记录
（含 14 张测试截图）、`test_videos/city.mp4`、`README.md`。

**没上传**（都在 `.gitignore` 里，且确认过历史里也没有）：

| 路径 | 大小 | 原因 |
|---|---|---|
| `dist/final_player/` | 934 MB | 打包产物，可重新生成 |
| `dist/final_player_win64.zip` | 409 MB | 分发包，同上 |
| `build/buildvenv/` | ~1 GB | 打包专用环境 |
| `.venv/` | —— | 开发环境 |
| `test_videos/*.mp4`（除 city.mp4） | —— | 用户自己的视频与转换产物 |

---

## 七、留下的东西 / 遗留

- **备份分支 `backup-20261016-pre-upload`**（指向重写前的 `8db483c`）：留在本地，**没有推送**。
  确认新历史没问题后可以删。
- **本地提交 hash 全部变了**：因为 rebase + filter-branch 重写了 21 个提交。
  之前记在文档 / 笔记里的短 hash（如 `02a22ab`、`b44a5c`、`f44a7b7`、`cb8e465`）
  都对应不上了 —— 内容没变，只是编号变了。
- **`.git` 仍是 124 MB**（那 329 MB 不可达对象还在）。想回收可以
  `git gc --prune=now`；本轮**没做**，因为保留它们能多一层后悔药。
- **`build/scrub_paths.py` 没有入库**（`build/` 被忽略）。它是可复用的脱敏工具，
  以后新写的文档若又带上本机路径可以直接再跑。要长期保留得挪到 `tools/` 并提交。
- **README 内容过时**：还写着「暂不支持播放列表、记忆播放位置、AB 循环、字幕」，
  但这三样第 14~21 轮都做了；项目结构一节也还是第 11 轮的样子（没有
  `media_tools.py` / `depth3d.py` / `tests/`）。本轮没改 —— 属于另一件事。

### 一条与隐私无关、但对用户有价值的提醒

提交作者是 `Kinen4062 <你的邮箱>` —— **邮箱会随每个提交公开**。
这是他自己 GitHub 账号的邮箱，属正常做法；如果不想暴露，可以在 GitHub 设置里开启
邮箱隐私并用 `noreply` 地址，但已推送的历史改不了。
