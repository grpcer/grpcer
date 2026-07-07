#!/usr/bin/env python3
"""聚合 grpcer 名下全部仓库(含 private)的语言字节数，生成 dark/light 两版语言分布 SVG。

私有仓库的源码内容不会被写入产物，只有聚合后的语言字节占比会出现在生成的 SVG 里。
"""
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = os.environ.get("OWNER", "grpcer")
TOKEN = os.environ["LANG_STATS_PAT"]

# profile README 仓库本身没有实质代码语言，排除
EXCLUDE_REPOS = {OWNER}

# 沿用 GitHub linguist 官方配色，观感和原生语言色点一致
LANG_COLORS = {
    "TypeScript": "#3178c6",
    "Swift": "#F05138",
    "Kotlin": "#A97BFF",
    "Go": "#00ADD8",
    "JavaScript": "#f1e05a",
    "HTML": "#e34c26",
    "CSS": "#563d7c",
    "Python": "#3572A5",
    "Shell": "#89e051",
    "C": "#555555",
    "Ruby": "#701516",
    "Other": "#8b949e",
}

THEMES = {
    "dark": {
        "title": "#2DD4BF",
        "text": "#C9D1D9",
        "sub": "#9BA1A6",
        "track": "#30363d",
    },
    "light": {
        "title": "#0D9488",
        "text": "#1F2937",
        "sub": "#4B5563",
        "track": "#e5e7eb",
    },
}


def api_get(path):
    req = urllib.request.Request(
        API + path,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"GitHub API {path} 失败: {e.code} {e.read().decode()}")


def list_owned_repos():
    repos, page = [], 1
    while True:
        batch = api_get(f"/user/repos?type=owner&per_page=100&page={page}")
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return [r for r in repos if not r["fork"] and r["name"] not in EXCLUDE_REPOS]


def aggregate_languages(repos):
    totals = {}
    for r in repos:
        langs = api_get(f"/repos/{OWNER}/{r['name']}/languages")
        for lang, size in langs.items():
            totals[lang] = totals.get(lang, 0) + size
    return totals


def top_n_with_other(totals, n=8):
    items = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = items[:n], items[n:]
    if tail:
        head.append(("Other", sum(size for _, size in tail)))
    return head


def build_svg(ranked, theme_name):
    theme = THEMES[theme_name]
    total = sum(size for _, size in ranked) or 1
    width = 850
    bar_h = 14
    row_h = 26
    cols = 2
    rows = -(-len(ranked) // cols)  # ceil
    legend_top = 70
    height = legend_top + rows * row_h + 20

    parts = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
    )
    parts.append(
        f'<text x="0" y="24" font-family="JetBrains Mono, monospace" font-size="18" '
        f'font-weight="700" fill="{theme["title"]}">Most Used Languages (incl. private repos)</text>'
    )

    # 顶部堆叠比例条（裁到圆角轨道内，避免首尾色块方角露出轨道外）
    bar_y = 40
    x = 0
    parts.append(
        f'<clipPath id="barclip-{theme_name}"><rect x="0" y="{bar_y}" width="{width}" height="{bar_h}" rx="{bar_h/2}"/></clipPath>'
    )
    parts.append(
        f'<rect x="0" y="{bar_y}" width="{width}" height="{bar_h}" rx="{bar_h/2}" fill="{theme["track"]}"/>'
    )
    parts.append(f'<g clip-path="url(#barclip-{theme_name})">')
    for lang, size in ranked:
        w = width * size / total
        color = LANG_COLORS.get(lang, LANG_COLORS["Other"])
        parts.append(f'<rect x="{x:.2f}" y="{bar_y}" width="{w:.2f}" height="{bar_h}" fill="{color}"/>')
        x += w
    parts.append("</g>")

    # 图例：两列
    col_w = width / cols
    for i, (lang, size) in enumerate(ranked):
        pct = 100 * size / total
        col, row = divmod(i, rows)
        cx = col * col_w
        cy = legend_top + row * row_h
        color = LANG_COLORS.get(lang, LANG_COLORS["Other"])
        parts.append(f'<circle cx="{cx+6}" cy="{cy-4}" r="5" fill="{color}"/>')
        parts.append(
            f'<text x="{cx+18}" y="{cy}" font-family="JetBrains Mono, monospace" font-size="13" '
            f'fill="{theme["text"]}">{lang}</text>'
        )
        parts.append(
            f'<text x="{cx+col_w-8}" y="{cy}" font-family="JetBrains Mono, monospace" font-size="13" '
            f'text-anchor="end" fill="{theme["sub"]}">{pct:.1f}%</text>'
        )

    parts.append("</svg>")
    return "\n".join(parts)


def main():
    repos = list_owned_repos()
    totals = aggregate_languages(repos)
    ranked = top_n_with_other(totals)

    os.makedirs("lang-stats", exist_ok=True)
    for theme_name in ("dark", "light"):
        svg = build_svg(ranked, theme_name)
        with open(f"lang-stats/lang-stats-{theme_name}.svg", "w", encoding="utf-8") as f:
            f.write(svg)

    print("聚合仓库:", ", ".join(r["name"] for r in repos))
    print("语言字节占比:", {k: v for k, v in ranked})


if __name__ == "__main__":
    main()
