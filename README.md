# awesome_papers

从 **arXiv** 论文链接自动抓取元数据（发表时间、机构、简称、项目页、GitHub 等），并输出为与 `ref.md` 约定一致的 **Markdown 表格**，便于维护个人论文清单。

## 功能概览

- 支持 `arxiv.org/abs/...` 与 `arxiv.org/pdf/...` 形式的链接  
- 输出列：`Year`、`Org.`、`Acronym`、`Paper`（标题 + 链接）、`Project`、`GitHub`（shields 徽章）、`Comments`  
- 输入方式：命令行参数、或 `input.json` 中的 URL 列表  
- 可选 **合并** 已有 `.md`：按 arXiv ID 去重，并按时间列 **重新排序**  

## 环境要求

- Python **3.10+**
- 依赖见 [`requirements.txt`](requirements.txt)

## 安装

```bash
cd awesome_papers
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 命令行用法

在仓库根目录（或指定脚本路径）执行 `paper_collection_agent.py`。

### 基本示例

```bash
# 单篇
python paper_collection_agent.py https://arxiv.org/abs/2309.17024

# 多篇
python paper_collection_agent.py \
  https://arxiv.org/pdf/2203.01577 \
  https://arxiv.org/abs/2406.09598

# 指定输出文件（默认不写则为 papers_collection.md）
python paper_collection_agent.py https://arxiv.org/abs/2309.17024 -o my_papers.md
```

### 从 JSON 读取链接

```bash
# 使用 -i 指定列表文件
python paper_collection_agent.py -i input.json -o collection.md
```

`input.json` 支持以下两种结构之一：

```json
{
  "papers": [
    "https://arxiv.org/pdf/2203.01577",
    "https://arxiv.org/abs/2309.17024"
  ]
}
```

或直接为 URL 数组：

```json
[
  "https://arxiv.org/abs/2309.17024"
]
```

若 **未传任何 URL** 且 **未使用 `-i`**，脚本会尝试读取与本脚本同目录下的默认文件 **`input.json`**（若存在）。

### 合并已有 Markdown 并排序

```bash
# 将新论文合并进已有表格：同 arXiv ID 以本次结果为准，全文按 Year 升序
python paper_collection_agent.py https://arxiv.org/abs/2401.08399 -o ref.md --merge

# 时间降序（新 → 旧）
python paper_collection_agent.py https://arxiv.org/abs/2401.08399 -o ref.md --merge --desc
```

### 参数说明

| 参数 | 说明 |
|------|------|
| `urls` | 零个或多个 arXiv 链接（位置参数） |
| `-i`, `--input-json` | 从 JSON 文件读取链接列表 |
| `-o`, `--output` | 输出 Markdown 路径（默认：`papers_collection.md`） |
| `--merge` | 若 `-o` 指向的文件已存在且含合法表头，则合并后再排序 |
| `--desc` | 按 Year 降序；默认升序 |

完整帮助：

```bash
python paper_collection_agent.py -h
```

## 输出格式

表头与分隔行与项目中的 **`ref.md`** 保持一致，例如：

```text
|Year|Org.|Acronym|Paper|Project|GitHub|Comments|
|----|----|-------|-----|-------|------|------|
```

自动抓取字段可能不完整（如机构、项目页、GitHub），对应单元格会留空，可事后手工补全。

## collection_list 变更 → 自动推送到 GitHub

脚本 **`collection_git_sync_agent.py`** 使用文件监听：当 `collection_list/` 下任意文件被创建、修改或删除时，在短暂静默（防抖，默认 3 秒）后，对**整个 `awesome_papers` 目录**执行 `git add -A`、`git commit`、`git push`。

### 一次性准备（在本目录初始化远程仓库）

```bash
cd /home/djy/awesome_papers
git init
git remote add origin git@github.com:<你的用户名>/<仓库名>.git   # 或 HTTPS URL
# 配置提交身份（若尚未全局配置）
git config user.email "you@example.com"
git config user.name "Your Name"
```

推送需已配置 **SSH 公钥**到 GitHub，或 **HTTPS + 凭据助手**（勿将密码写入仓库；`user.json` 等敏感文件已列入 `.gitignore`）。

### 运行监听（前台）

```bash
cd /home/djy/awesome_papers
pip install -r requirements.txt   # 含 watchdog
python collection_git_sync_agent.py
```

可选环境变量：

| 变量 | 含义 |
|------|------|
| `AWESOME_PAPERS_ROOT` | 仓库根路径，默认为本脚本所在目录 |
| `SYNC_DEBOUNCE_SEC` | 防抖秒数，默认 `3` |
| `GIT_REMOTE` | 远程名，默认 `origin` |
| `GIT_BRANCH` | 分支名；不设置则使用当前 `HEAD` 分支 |

### 后台长期运行（可选）

使用 `tmux`/`screen`，或 systemd 用户服务，在机器登录后自动启动上述命令即可。

## 说明与限制

- 依赖 **arXiv API**、**OpenAlex**、页面解析及 **GitHub** 公开接口；请保证网络可访问，必要时重试（arXiv 偶发超时）。  
- `Org.`、`Project`、`GitHub` 等为启发式推断，不保证与论文主页完全一致，重要条目建议人工核对。  
- **请勿**将账号密码、Token 提交到 Git；若曾将含密钥的 `user.json` 提交过，请轮换密钥并从历史中移除敏感文件。  

## 许可证

未指定许可证时，默认仅作个人学习与整理使用；若对外发布请自行补充 `LICENSE`。
