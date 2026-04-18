#!/usr/bin/env python3
"""
Paper Collection Agent
======================
输入一篇或多篇论文链接（arXiv abs/pdf），输出与 ref.md 同格式的 Markdown 表格；
可写入新文件或合并已有 .md，并按时间（Year 字段）排序。

输入方式：
  - 命令行参数直接传入链接
  - 或从 JSON 读取（默认 awesome_papers/input.json）

示例：
  python paper_collection_agent.py https://arxiv.org/pdf/2203.01577
  python paper_collection_agent.py -i input.json -o collection.md
  python paper_collection_agent.py URL1 URL2 -o ref.md --merge
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "PaperCollectionAgent/1.0 (research tool)"}

# 与 ref.md 一致的表头与分隔行
TABLE_HEADER = (
    "|Year|Org.|Acronym|Paper|Project|GitHub|Comments|\n"
    "|----|----|-------|-----|-------|------|------|"
)

DEFAULT_INPUT_JSON = Path(__file__).resolve().parent / "input.json"


# ---------------------------------------------------------------------------
# arXiv ID
# ---------------------------------------------------------------------------

def parse_arxiv_id(raw: str) -> Optional[str]:
    patterns = [
        r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})(?:\.pdf)?",
        r"^(\d{4}\.\d{4,5})$",
    ]
    for p in patterns:
        m = re.search(p, raw.strip())
        if m:
            return m.group(1)
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


def fetch_openalex_org(title: str) -> str:
    try:
        resp = requests.get(
            "https://api.openalex.org/works",
            params={"search": title[:120], "per-page": 1, "mailto": "paper-agent@localhost"},
            headers=HEADERS,
            timeout=20,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return ""
        work = results[0]
        found_title = (work.get("title") or "").lower()
        if title.lower()[:30] not in found_title:
            return ""
        for authorship in work.get("authorships", []):
            institutions = authorship.get("institutions", [])
            if institutions:
                return institutions[0].get("display_name", "")
    except Exception:
        pass
    return ""


def scrape_arxiv_affiliations(arxiv_id: str) -> str:
    url = f"https://arxiv.org/html/{arxiv_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        if resp.status_code != 200:
            return ""
        soup = BeautifulSoup(resp.text, "html.parser")
        for cls in ("ltx_role_affiliation", "ltx_affiliation", "author-info"):
            tags = soup.find_all(class_=cls)
            if tags:
                text = tags[0].get_text(" ").strip()
                text = re.sub(r"^[\d†‡*]+\s*", "", text).strip()
                if len(text) > 3:
                    return text
        for tag in soup.find_all(True):
            cls_str = " ".join(tag.get("class", []))
            id_str = tag.get("id", "")
            if "affil" in cls_str.lower() or "affil" in id_str.lower():
                text = tag.get_text(" ").strip()
                if 5 < len(text) < 200:
                    return text
    except Exception:
        pass
    return ""


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


def infer_acronym(title: str) -> str:
    m = re.search(r"\(([A-Z][A-Z0-9\-]{1,12})\)", title)
    if m:
        return m.group(1)
    before_colon = title.split(":")[0].strip()
    tokens = before_colon.split()
    if tokens:
        first = tokens[0]
        if first.isupper() and len(first) >= 2:
            return first
        if re.match(r"^[A-Z][a-z]+(?:[A-Z][a-z0-9]*)+$", first) and len(first) >= 4:
            return first
        if re.match(r"^[A-Z][A-Z0-9\-]{2,}$", first):
            return first
    # e.g. "Introducing HOT3D: ..." / "HOI4D: ..." 中的字母数字混合缩写
    m2 = re.search(r"\b([A-Z]{2,}\d+[A-Z0-9]*)\b", title)
    if m2:
        return m2.group(1)
    return ""


def shorten_org(org: str) -> str:
    replacements = [
        ("Massachusetts Institute of Technology", "MIT"),
        ("Carnegie Mellon University", "CMU"),
        ("Shanghai Artificial Intelligence Laboratory", "ShanghaiAI"),
        ("Shanghai AI Laboratory", "ShanghaiAI"),
        ("Stanford University", "Stanford"),
        ("University of California", "UC"),
        ("Tsinghua University", "Tsinghua"),
        ("Peking University", "PKU"),
        ("Zhejiang University", "ZJU"),
        ("Fudan University", "Fudan"),
        ("ETH Zurich", "ETH"),
        ("Microsoft Research", "Microsoft"),
        ("Google Research", "Google"),
        ("Google DeepMind", "DeepMind"),
        ("Meta AI", "Meta"),
        ("Facebook AI Research", "Meta"),
        ("Apple Inc", "Apple"),
        ("NVIDIA", "NVIDIA"),
        ("Samsung Research", "Samsung"),
        ("ByteDance", "ByteDance"),
        ("Baidu", "Baidu"),
        ("Alibaba", "Alibaba"),
        ("Tencent", "Tencent"),
        ("Amazon", "Amazon"),
        ("Adobe", "Adobe"),
    ]
    org_lower = org.lower()
    for long, short in replacements:
        if long.lower() in org_lower:
            return short
    org = re.sub(r"\s*\([^)]*\)\s*$", "", org).strip()
    return org.split(",")[0].split(";")[0].strip()


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
    """返回一条表格行所需字段 + arxiv_id 用于去重与排序。"""
    arxiv_id = parse_arxiv_id(raw_input_url)
    if not arxiv_id:
        raise ValueError(f"无法解析 arXiv ID: {raw_input_url!r}")

    api_data = fetch_arxiv_api(arxiv_id)
    title = api_data["title"]
    year_month = api_data["year_month"]
    comment = api_data["comment"]
    authors = api_data["authors"]

    comment_links = extract_links_from_comment(comment)
    page_links = scrape_arxiv_page(arxiv_id)
    github = comment_links.get("github") or page_links.get("github")
    project = comment_links.get("project") or page_links.get("project")
    acronym = infer_acronym(title)

    org = ""
    for a in authors:
        if a.get("affiliation"):
            org = shorten_org(a["affiliation"])
            break
    if not org:
        raw_affil = fetch_openalex_org(title)
        if raw_affil:
            org = shorten_org(raw_affil)
    if not org:
        raw_affil = scrape_arxiv_affiliations(arxiv_id)
        if raw_affil:
            org = shorten_org(raw_affil)

    if not github and project:
        github = scrape_project_page_for_github(project)
    if not github:
        github = search_github(acronym, title, project_url=project)

    paper_href = paper_url_for_row(arxiv_id, raw_input_url)
    paper_link = f"[{title}]({paper_href})"
    project_cell = format_project_badge(project) if project else ""
    github_cell = format_github_badge(github) if github else ""

    # 与 ref.md 示例行一致：Year | Acronym | Org. | Paper | Project | GitHub | Comments
    row = (
        f"|{year_month}| {acronym} |{org}| {paper_link} |{project_cell} |{github_cell} | |"
    )
    return {
        "arxiv_id": arxiv_id,
        "year_month": year_month,
        "row": row,
    }


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
    合并已有表格与新条目，按 arxiv_id 去重（新覆盖旧），再按 Year 排序。
    """
    _, old_rows = split_table_lines(existing_content)
    by_id: dict[str, str] = {}

    for ln in old_rows:
        aid = arxiv_id_from_markdown_row(ln)
        if aid:
            by_id[aid] = ln
        else:
            # 无 arxiv 链接的行保留为匿名行，用行内容做 key 防重复
            by_id[f"__anon_{hash(ln)}"] = ln

    for ent in new_entries:
        by_id[ent["arxiv_id"]] = ent["row"]

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
    raise ValueError("input.json 应为 {\"papers\": [\"url\", ...]} 或 URL 数组")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="从 arXiv 链接收集论文信息，输出 ref.md 风格表格；可合并已有 md 并按时间排序。",
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
        help=f"从 JSON 读取链接列表（默认: {DEFAULT_INPUT_JSON}，与 -i 互斥时可仅用参数）",
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
        action="store_true",
        help="若输出文件已存在，读取其中表格并与本次结果合并，按 arXiv ID 去重后按时间排序",
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
            parser.error("请传入至少一个 URL，或使用 -i 指定 input.json（且默认文件不存在）")
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
            entries.append(collect_one(raw))
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
