#!/usr/bin/env python3
"""
Paper Collection Agent
======================
输入一篇或多篇论文链接（arXiv abs/pdf），输出与 ref.md 同格式的 Markdown 表格；
默认若 `-o` 已存在则**合并**已有 .md 再排序；加 `--no-merge` 才覆盖写入。

表格列顺序：Year | Org. | Acronym | Paper | Project | GitHub | Comments
  - Org.：默认留空（可自行在表格中填写）。
  - Acronym：标题中首个半角「:」或全角「：」**之前**的整段文字（论文项目名/简称）；若无冒号，则由脚本根据标题启发式生成简称。

输入方式：
  - 命令行参数直接传入链接（arXiv abs/pdf，或 GitHub 仓库 URL）
  - 或从 JSON 读取（默认 awesome_papers/input/simgen.json）

GitHub 输入：从仓库描述、homepage、README 中解析 arXiv；若找不到论文，Paper 列留空，Year 取仓库创建时间，Acronym 取仓库名。

示例：
  python paper_collection_agent.py https://arxiv.org/pdf/2203.01577
  python paper_collection_agent.py -i input.json -o collection.md
  python paper_collection_agent.py URL1 URL2 -o ref.md
  python paper_collection_agent.py URL1 URL2 -o ref.md --no-merge   # 覆盖已有文件，不合并
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional, Tuple
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "PaperCollectionAgent/1.0 (research tool)"}

# 与 ref.md 一致的表头与分隔行
TABLE_HEADER = (
    "|Year|Org.|Acronym|Paper|Project|GitHub|Comments|\n"
    "|----|----|-------|-----|-------|------|------|"
)

DEFAULT_INPUT_JSON = Path(__file__).resolve().parent / "input" / "simgen.json"


# ---------------------------------------------------------------------------
# arXiv ID
# ---------------------------------------------------------------------------

def parse_arxiv_id(raw: str) -> Optional[str]:
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:\.pdf)?",
        r"arxiv\s*:\s*(\d{4}\.\d{4,5})",
        r"^(\d{4}\.\d{4,5})$",
    ]
    for p in patterns:
        m = re.search(p, raw.strip(), re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def find_arxiv_id_in_text(text: str) -> Optional[str]:
    """从任意文本中提取首个 arXiv ID（用于 GitHub 描述/README 等）。"""
    if not text:
        return None
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:\.pdf)?",
        r"arxiv\s*:\s*(\d{4}\.\d{4,5})",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def parse_github_repo_url(raw: str) -> Optional[Tuple[str, str]]:
    """
    从 GitHub 仓库 URL 解析 (owner, repo)。
    支持 https://github.com/o/r、/tree/、.git 等。
    """
    s = raw.strip()
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/?#]+?)(?:\.git)?(?:/|$)",
        s,
        re.IGNORECASE,
    )
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    if repo.lower() in ("tree", "blob", "pull", "issues", "discussions"):
        return None
    return owner, repo


def github_row_key(owner: str, repo: str) -> str:
    return f"{owner}/{repo}".lower()


def fetch_github_repo_api(owner: str, repo: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    resp = requests.get(
        url,
        headers={**HEADERS, "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_github_readme_text(owner: str, repo: str) -> str:
    """获取默认分支 README 纯文本（失败则返回空串）。"""
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{owner}/{repo}/readme",
            headers={**HEADERS, "Accept": "application/vnd.github+json"},
            timeout=25,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        dl = data.get("download_url")
        if not dl:
            return ""
        r2 = requests.get(dl, headers=HEADERS, timeout=25)
        if r2.status_code != 200:
            return ""
        return r2.text
    except Exception:
        return ""


def github_repo_key_from_markdown_row(row_line: str) -> Optional[str]:
    """从表格行中提取 github.com/owner/repo 用于去重（小写 owner/repo）。"""
    for m in re.finditer(
        r"github\.com/([^/]+)/([^/\"'>\s?#]+)",
        row_line,
        re.IGNORECASE,
    ):
        owner, repo = m.group(1), m.group(2)
        if repo.lower().rstrip("/").endswith(".git"):
            repo = repo[:-4]
        if owner.lower() == "repos":
            continue
        return github_row_key(owner, repo)
    return None


def paper_url_for_row(arxiv_id: str, original: str) -> str:
    """若用户给的是 pdf 链接则输出 pdf（与 ref.md 常见写法一致，无 .pdf 后缀）。"""
    if re.search(r"arxiv\.org/pdf/", original, re.I):
        return f"https://arxiv.org/pdf/{arxiv_id}"
    return f"https://arxiv.org/abs/{arxiv_id}"


# ---------------------------------------------------------------------------
# Fetch metadata (arXiv + OpenAlex + scrape)
# ---------------------------------------------------------------------------

def fetch_arxiv_api(arxiv_id: str) -> dict:
    url = f"http://export.arxiv.org/api/query?id_list={arxiv_id}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    root = ET.fromstring(resp.text)
    entry = root.find("atom:entry", ns)
    if entry is None:
        raise ValueError(f"arXiv API returned no entry for id={arxiv_id}")

    title = entry.find("atom:title", ns).text.strip().replace("\n", " ")
    title = re.sub(r"\s+", " ", title)
    published = entry.find("atom:published", ns).text
    year_month = published[:7].replace("-", ".")
    comment_el = entry.find("arxiv:comment", ns)
    comment = comment_el.text.strip() if comment_el is not None else ""

    authors = []
    for author_el in entry.findall("atom:author", ns):
        name = author_el.find("atom:name", ns).text
        affil_el = author_el.find("arxiv:affiliation", ns)
        affil = affil_el.text.strip() if affil_el is not None else ""
        authors.append({"name": name, "affiliation": affil})

    return {
        "title": title,
        "year_month": year_month,
        "comment": comment,
        "authors": authors,
    }


def scrape_arxiv_page(arxiv_id: str) -> dict:
    url = f"https://arxiv.org/abs/{arxiv_id}"
    github = project = None
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception:
        return {"github": None, "project": None}

    candidate_text = ""
    for tag in soup.find_all(["blockquote", "td", "p"]):
        candidate_text += " " + tag.get_text(" ")
    all_hrefs = [a["href"] for a in soup.find_all("a", href=True)]

    for raw in re.findall(r"https?://github\.com/[^\s<>\"')]+", candidate_text):
        clean = raw.rstrip(".,;)")
        if re.match(r"https?://github\.com/[^/]+/[^/]+", clean):
            github = clean
            break
    if not github:
        for href in all_hrefs:
            if "github.com" in href and re.match(r"https?://github\.com/[^/]+/[^/]+", href):
                github = href.rstrip("/")
                break

    project_domains = (
        "github.io",
        "gitlab.io",
        "sites.google",
        "huggingface.co/spaces",
        "huggingface.co/datasets",
    )
    for href in all_hrefs:
        if any(d in href for d in project_domains) and "github.com" not in href:
            if href.startswith("http"):
                project = href.rstrip("/")
                break
    if not project:
        for pat in [
            r"project\s+page[:\s]+(https?://\S+)",
            r"(https?://[^\s<>\"')]+(?:github\.io|gitlab\.io|sites\.google)[^\s<>\"')]*)",
            r"homepage[:\s]+(https?://\S+)",
        ]:
            m = re.search(pat, candidate_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).rstrip(".,;)")
                if "github.com" not in candidate:
                    project = candidate
                    break

    return {"github": github, "project": project}


def github_owner_from_project_page(project_url: str) -> Optional[str]:
    m = re.match(r"https?://([^.]+)\.github\.io", project_url or "")
    return m.group(1) if m else None


def search_github(acronym: str, title: str, project_url: Optional[str] = None) -> Optional[str]:
    def _is_website_repo(repo_name: str) -> bool:
        return repo_name.endswith(".github.io") or repo_name.endswith(".gitlab.io")

    owner = github_owner_from_project_page(project_url)
    if owner and acronym:
        needle = acronym.lower()
        try:
            resp = requests.get(
                f"https://api.github.com/users/{owner}/repos",
                params={"per_page": 30, "sort": "updated"},
                headers={**HEADERS, "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if resp.status_code == 200:
                for repo in resp.json():
                    repo_name = repo.get("name", "")
                    if _is_website_repo(repo_name):
                        continue
                    if needle in repo_name.lower():
                        return repo["html_url"]
        except Exception:
            pass
        time.sleep(0.3)

    def _name_matches(repo_name: str, needle: str) -> bool:
        tokens = re.split(r"[-_. ]", repo_name.lower())
        return needle.lower() in tokens

    stop = {"a", "an", "the", "of", "for", "in", "on", "and", "with", "to"}
    words = [w for w in title.split() if w.lower() not in stop][:4]
    queries = []
    if acronym:
        queries.append(f"{acronym} in:name")
    queries.append(f"{' '.join(words)} in:name,description")

    for q in queries:
        try:
            resp = requests.get(
                "https://api.github.com/search/repositories",
                params={"q": q, "sort": "stars", "per_page": 10},
                headers={**HEADERS, "Accept": "application/vnd.github+json"},
                timeout=15,
            )
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                needle = acronym if acronym else words[0]
                for item in items:
                    repo_name = item.get("name", "")
                    if _is_website_repo(repo_name):
                        continue
                    if _name_matches(repo_name, needle):
                        return item["html_url"]
            elif resp.status_code == 403:
                break
            time.sleep(0.5)
        except Exception:
            continue
    return None


def scrape_project_page_for_github(project_url: str) -> Optional[str]:
    if not project_url:
        return None
    try:
        resp = requests.get(project_url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "github.com" in href and re.match(r"https?://github\.com/[^/]+/[^/]+", href):
                repo_name = href.rstrip("/").split("/")[-1]
                if not repo_name.endswith(".github.io"):
                    return href.rstrip("/")
    except Exception:
        pass
    return None


def extract_links_from_comment(comment: str) -> dict:
    github = project = None
    for raw in re.findall(r"https?://[^\s<>\"']+", comment):
        clean = raw.rstrip(".,;)")
        if "github.com" in clean and not github:
            if re.match(r"https?://github\.com/[^/]+/[^/]+", clean):
                github = clean
        elif not project and "github.com" not in clean:
            project = clean
    return {"github": github, "project": project}


def split_title_at_colon(title: str) -> tuple[str, bool]:
    """
    按标题中首个半角 ':' 或全角 '：' 分割。
    返回 (冒号前的文字, 是否包含冒号)。
    """
    m = re.search(r"[:：]", title)
    if not m:
        return title.strip(), False
    return title[: m.start()].strip(), True


def _acronym_fallback_no_colon(title: str) -> str:
    """标题无冒号时：括号缩写 → 字母数字混合名 → CamelCase/全大写词 → 首词或前几词首字母。"""
    t = title.strip()
    if not t:
        return ""

    m = re.search(r"\(([A-Z][A-Z0-9\-]{1,15})\)", t)
    if m:
        return m.group(1)

    m2 = re.search(r"\b([A-Z]{2,}\d+[A-Z0-9]*)\b", t)
    if m2:
        return m2.group(1)

    for word in re.findall(r"[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+|[A-Z][A-Z0-9]{2,}", t):
        if len(word) >= 3:
            return word

    words = re.findall(r"[A-Za-z][a-zA-Z0-9\-]*", t)
    if not words:
        return t[:48].strip()

    if len(words) == 1:
        w = words[0]
        return w if len(w) <= 24 else w[:24]

    initials = "".join(w[0].upper() for w in words[:5])
    if 2 <= len(initials) <= 10:
        return initials

    return words[0][:32]


def infer_acronym(title: str) -> str:
    """
    Acronym：有冒号时取冒号前整段（多空格压成单空格）；
    无冒号或冒号前为空时由 _acronym_fallback_no_colon 总结。
    """
    before, has_colon = split_title_at_colon(title)
    if has_colon and before:
        return re.sub(r"\s+", " ", before).strip()
    return _acronym_fallback_no_colon(title)


def normalize_github_repo_url(url: str) -> str:
    """去掉 /blob/、/tree/ 等路径，保留仓库根 URL。"""
    m = re.match(r"(https?://github\.com/[^/]+/[^/]+)", url.rstrip("/"))
    return m.group(1) if m else url.rstrip("/")


def format_github_badge(github_url: str) -> str:
    clean = normalize_github_repo_url(github_url)
    m = re.match(r"https?://github\.com/([^/]+/[^/?#]+)", clean)
    if not m:
        return ""
    repo = m.group(1).rstrip("/")
    return f"[![GitHub](https://img.shields.io/github/stars/{repo})]({clean})"


def format_project_badge(project_url: str) -> str:
    return f"[![link](https://img.shields.io/badge/Website-9cf)]({project_url})"


def collect_one(raw_input_url: str) -> dict:
    """返回一条表格行所需字段 + row_id 用于去重与排序（arXiv 输入）。"""
    arxiv_id = parse_arxiv_id(raw_input_url)
    if not arxiv_id:
        raise ValueError(f"无法解析 arXiv ID: {raw_input_url!r}")

    api_data = fetch_arxiv_api(arxiv_id)
    title = api_data["title"]
    year_month = api_data["year_month"]
    comment = api_data["comment"]

    comment_links = extract_links_from_comment(comment)
    page_links = scrape_arxiv_page(arxiv_id)
    github = comment_links.get("github") or page_links.get("github")
    project = comment_links.get("project") or page_links.get("project")
    acronym = infer_acronym(title)

    # Org.：默认留空（避免与 Acronym 混淆；可在生成的 md 中手工填写）
    org = ""

    if not github and project:
        github = scrape_project_page_for_github(project)
    if not github:
        github = search_github(acronym, title, project_url=project)

    paper_href = paper_url_for_row(arxiv_id, raw_input_url)
    paper_link = f"[{title}]({paper_href})"
    project_cell = format_project_badge(project) if project else ""
    github_cell = format_github_badge(github) if github else ""

    # 与表头一致：Year | Org. | Acronym | Paper | Project | GitHub | Comments
    row = (
        f"|{year_month}| {org} | {acronym} | {paper_link} |{project_cell} |{github_cell} | |"
    )
    return {
        "row_id": f"arxiv:{arxiv_id}",
        "year_month": year_month,
        "row": row,
    }


def collect_from_github(raw_input_url: str) -> dict:
    """
    GitHub 仓库 URL：从描述 / homepage / README 解析 arXiv；
    若找到论文则 Paper 等与 arXiv 行一致（GitHub 列固定为当前仓库）；
    否则 Paper 留空，Year 为仓库创建年月，Acronym 为仓库名。
    """
    parsed = parse_github_repo_url(raw_input_url)
    if not parsed:
        raise ValueError(f"无法解析 GitHub 仓库 URL: {raw_input_url!r}")

    owner, repo_slug = parsed
    repo_json = fetch_github_repo_api(owner, repo_slug)
    repo_name = repo_json.get("name") or repo_slug
    description = (repo_json.get("description") or "").strip()
    homepage = (repo_json.get("homepage") or "").strip()

    readme = fetch_github_readme_text(owner, repo_slug)
    blob = "\n".join(x for x in (description, homepage, readme) if x)

    arxiv_id = find_arxiv_id_in_text(blob) or parse_arxiv_id(homepage)
    org = ""
    user_github = normalize_github_repo_url(f"https://github.com/{owner}/{repo_name}")
    github_cell = format_github_badge(user_github)

    row_key = f"gh:{github_row_key(owner, repo_name)}"

    if arxiv_id:
        api_data = fetch_arxiv_api(arxiv_id)
        title = api_data["title"]
        year_month = api_data["year_month"]
        comment = api_data["comment"]
        acronym = infer_acronym(title)

        comment_links = extract_links_from_comment(comment)
        page_links = scrape_arxiv_page(arxiv_id)
        project = comment_links.get("project") or page_links.get("project")
        if not project and homepage:
            hp = homepage.rstrip("/")
            if (
                re.match(r"https?://", hp, re.I)
                and "arxiv.org" not in hp.lower()
                and normalize_github_repo_url(hp) != user_github
            ):
                project = hp

        paper_href = paper_url_for_row(arxiv_id, f"https://arxiv.org/abs/{arxiv_id}")
        paper_link = f"[{title}]({paper_href})"
        project_cell = format_project_badge(project) if project else ""
        row = (
            f"|{year_month}| {org} | {acronym} | {paper_link} |{project_cell} |{github_cell} | |"
        )
        return {"row_id": row_key, "year_month": year_month, "row": row}

    # 无 arXiv：Paper 为空
    created = repo_json.get("created_at") or ""
    if created and len(created) >= 7:
        year_month = created[:7].replace("-", ".")
    else:
        year_month = "9999.99"

    acronym = repo_name
    project_cell = ""
    if homepage:
        hp = homepage.rstrip("/")
        if re.match(r"https?://", hp, re.I) and "arxiv.org" not in hp.lower():
            if normalize_github_repo_url(hp) != user_github:
                project_cell = format_project_badge(hp)

    paper_link = ""
    row = f"|{year_month}| {org} | {acronym} | {paper_link} |{project_cell} |{github_cell} | |"
    return {"row_id": row_key, "year_month": year_month, "row": row}


def collect_entry(raw_input_url: str) -> dict:
    """根据 URL 类型分发：GitHub 仓库或 arXiv。"""
    if parse_github_repo_url(raw_input_url):
        return collect_from_github(raw_input_url)
    return collect_one(raw_input_url)


# ---------------------------------------------------------------------------
# Markdown 解析 / 合并 / 排序
# ---------------------------------------------------------------------------

def arxiv_id_from_markdown_row(row_line: str) -> Optional[str]:
    """从表格数据行中提取 arxiv.org/abs 或 pdf 链接里的 ID。"""
    m = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", row_line)
    return m.group(1) if m else None


def parse_year_sort_key(year_cell: str) -> tuple:
    """将 '2024.01' 转为可排序元组；无法解析放最后。"""
    year_cell = year_cell.strip()
    m = re.match(r"^(\d{4})\.(\d{1,2})$", year_cell)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m2 = re.match(r"^(\d{4})$", year_cell)
    if m2:
        return (int(m2.group(1)), 0)
    return (9999, 99)


def split_table_lines(content: str) -> tuple[list[str], list[str]]:
    """
    返回 (header+sep 行列表, 数据行列表)。
    若文件无有效表头，则整文件视为无表格。
    """
    lines = [ln.rstrip("\n") for ln in content.strip().splitlines()]
    if len(lines) < 2:
        return [], []
    # 识别以 |Year| 开头的表头
    hi = None
    for i, ln in enumerate(lines):
        if ln.strip().startswith("|Year|") and "Acronym" in ln:
            hi = i
            break
    if hi is None:
        return [], []
    header_lines = lines[hi : hi + 2]
    if len(lines) <= hi + 2:
        return header_lines, []
    data_lines = [ln for ln in lines[hi + 2 :] if ln.strip().startswith("|") and ln.strip() != "|"]
    return header_lines, data_lines


def merge_and_sort(
    existing_content: str,
    new_entries: list[dict],
    *,
    sort_ascending: bool = True,
) -> str:
    """
    合并已有表格与新条目，按 row_id 去重（arXiv ID 或 GitHub owner/repo，新覆盖旧），再按 Year 排序。
    """
    _, old_rows = split_table_lines(existing_content)
    by_id: dict[str, str] = {}

    for ln in old_rows:
        aid = arxiv_id_from_markdown_row(ln)
        if aid:
            by_id[f"arxiv:{aid}"] = ln
        else:
            ghk = github_repo_key_from_markdown_row(ln)
            if ghk:
                by_id[f"gh:{ghk}"] = ln
            else:
                by_id[f"__anon_{hash(ln)}"] = ln

    for ent in new_entries:
        by_id[ent["row_id"]] = ent["row"]

    def row_year(line: str) -> tuple:
        parts = line.split("|")
        if len(parts) >= 2:
            return parse_year_sort_key(parts[1])
        return (9999, 99)

    merged_lines = list(by_id.values())
    merged_lines.sort(key=row_year, reverse=not sort_ascending)

    out = TABLE_HEADER + "\n" + "\n".join(merged_lines)
    if not out.endswith("\n"):
        out += "\n"
    return out


def load_urls_from_json(path: Path) -> list[str]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return [str(x).strip() for x in data if str(x).strip()]
    if isinstance(data, dict):
        papers = data.get("papers") or data.get("urls") or data.get("links")
        if isinstance(papers, list):
            return [str(x).strip() for x in papers if str(x).strip()]
    raise ValueError("JSON 应为 {\"papers\": [\"url\", ...]} 或 URL 数组")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 arXiv 或 GitHub 仓库链接收集论文信息，输出 ref.md 风格表格；可合并已有 md 并按时间排序。",
    )
    parser.add_argument(
        "urls",
        nargs="*",
        help="一篇或多篇 arXiv 链接（abs 或 pdf）",
    )
    parser.add_argument(
        "-i",
        "--input-json",
        type=Path,
        default=None,
        help=f"从 JSON 读取链接列表（默认存在则用: {DEFAULT_INPUT_JSON}）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("papers_collection.md"),
        help="输出 Markdown 路径（默认 papers_collection.md）",
    )
    parser.add_argument(
        "--merge",
        default=True,
        action=argparse.BooleanOptionalAction,
        help="若输出文件已存在，读取其中表格并与本次结果合并、去重后排序（默认开启）；"
        "使用 --no-merge 则直接覆盖输出文件，仅保留本次条目",
    )
    parser.add_argument(
        "--desc",
        action="store_true",
        help="按时间降序排序（默认升序：从早到晚）",
    )
    args = parser.parse_args()

    urls: list[str] = []
    if args.input_json is not None:
        urls.extend(load_urls_from_json(args.input_json))
    elif not args.urls:
        if DEFAULT_INPUT_JSON.is_file():
            urls.extend(load_urls_from_json(DEFAULT_INPUT_JSON))
        else:
            parser.error("请传入至少一个 URL，或使用 -i 指定 JSON（且默认 input/simgen.json 不存在）")
    urls.extend(args.urls)

    # 去重保持顺序
    seen = set()
    uniq_urls = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            uniq_urls.append(u)

    if not uniq_urls:
        print("错误: 没有可用的论文链接。", file=sys.stderr)
        sys.exit(1)

    entries: list[dict] = []
    for raw in uniq_urls:
        print(f"[•] {raw}")
        try:
            entries.append(collect_entry(raw))
        except Exception as e:
            print(f"    失败: {e}", file=sys.stderr)

    if not entries:
        sys.exit(1)

    sort_asc = not args.desc
    out_path = args.output
    existing = ""
    if args.merge and out_path.is_file():
        existing = out_path.read_text(encoding="utf-8")

    if args.merge and existing.strip():
        body = merge_and_sort(existing, entries, sort_ascending=sort_asc)
    else:
        # 仅本次条目排序
        entries.sort(
            key=lambda e: parse_year_sort_key(e["year_month"]),
            reverse=not sort_asc,
        )
        body = TABLE_HEADER + "\n" + "\n".join(e["row"] for e in entries) + "\n"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(body, encoding="utf-8")
    data_rows = len([ln for ln in body.splitlines() if ln.startswith("|") and not ln.startswith("|Year|") and not ln.startswith("|----")])
    print(f"[✓] 已写入 {out_path}（本次处理 {len(entries)} 条；表格共 {data_rows} 行数据）")


if __name__ == "__main__":
    main()
