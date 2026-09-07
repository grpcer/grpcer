#!/usr/bin/env python3
"""聚合 grpcer 名下全部仓库(含 private)的活跃度与语言字节数，渲染 assets/stats.svg。

私有仓库的源码内容不会被写入产物，只有聚合后的计数与语言字节占比会出现在生成的 SVG 里。
统计口径是 GitHub linguist 的**字节数**——UI 代码天然比后端代码体积大，所以卡片标题写明了口径。
拿不到的指标一律渲染成 "—"，不拿 0 冒充真实值。
"""
import datetime as dt
import json
import os
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = os.environ.get("OWNER", "grpcer")
TOKEN = os.environ["LANG_STATS_PAT"]

# profile README 仓库本身没有实质代码语言，排除
EXCLUDE_REPOS = {OWNER}

# 主力语言：在语言条里高亮成主色，其余按 linguist 官方色
MAIN_STACK = "Go"

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
    "C": "#6e7681",
    "Ruby": "#701516",
    "Other": "#8b949e",
}

WIDTH = 880
SEGMENTS = 28


# ---------------------------------------------------------------- GitHub API

def rest_get(path):
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


def graphql(query, variables):
    """契约数据拿不到时返回 None，由调用方降级成 "—"，不要中断整张图的渲染。"""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API + "/graphql",
        data=body,
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.HTTPError, urllib.error.URLError) as e:
        print(f"[warn] GraphQL 请求失败，活跃度指标降级为暂无: {e}")
        return None
    if payload.get("errors"):
        print(f"[warn] GraphQL 返回错误，活跃度指标降级为暂无: {payload['errors']}")
        return None
    return payload.get("data")


CALENDAR_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def fetch_contribution_days(login, frm, to):
    data = graphql(CALENDAR_QUERY, {
        "login": login,
        "from": frm.isoformat() + "T00:00:00Z",
        "to": to.isoformat() + "T23:59:59Z",
    })
    if not data or not data.get("user"):
        return None
    cal = data["user"]["contributionsCollection"]["contributionCalendar"]
    days = {}
    for week in cal["weeks"]:
        for day in week["contributionDays"]:
            days[day["date"]] = day["contributionCount"]
    return cal["totalContributions"], days


def activity_metrics(login):
    """返回 (当前连续提交天数, 今年贡献数)；任一拿不到就是 None。"""
    today = dt.date.today()
    # contributionsCollection 一次最多查一年，连续天数可能跨年，所以查两段再合并
    this_year = fetch_contribution_days(login, today - dt.timedelta(days=364), today)
    if this_year is None:
        return None, None
    _, days = this_year
    prev = fetch_contribution_days(login, today - dt.timedelta(days=729), today - dt.timedelta(days=365))
    if prev is not None:
        days = {**prev[1], **days}

    ytd = fetch_contribution_days(login, dt.date(today.year, 1, 1), today)
    ytd_total = ytd[0] if ytd is not None else None

    # 今天还没提交不算断档，从昨天起算
    cursor = today
    if days.get(cursor.isoformat(), 0) == 0:
        cursor -= dt.timedelta(days=1)
    streak = 0
    while days.get(cursor.isoformat(), 0) > 0:
        streak += 1
        cursor -= dt.timedelta(days=1)
    return streak, ytd_total


def list_owned_repos():
    repos, page = [], 1
    while True:
        batch = rest_get(f"/user/repos?type=owner&per_page=100&page={page}")
        if not batch:
            break
        repos.extend(batch)
        page += 1
    return [r for r in repos if not r["fork"] and r["name"] not in EXCLUDE_REPOS]


def aggregate_languages(repos):
    totals = {}
    for r in repos:
        for lang, size in rest_get(f"/repos/{OWNER}/{r['name']}/languages").items():
            totals[lang] = totals.get(lang, 0) + size
    return totals


def top_n_with_other(totals, n=7):
    items = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    head, tail = items[:n], items[n:]
    if tail:
        head.append(("Other", sum(size for _, size in tail)))
    return head


# ---------------------------------------------------------------- rendering

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def metric_card(x, y, w, label, value, unit, bar_pct, color, glow, bar_color=None):
    """一张指标卡：小标签 + 大数字 + 一条细进度线。value 为 None 时诚实显示 "—"。"""
    shown = "—" if value is None else f"{value:,}"
    if value is None:
        unit = "no data yet"
    value_fill = color if value is not None else "#3d4c5c"
    bar_color = bar_color or color
    filt = f' filter="url(#{glow})"' if (value is not None and glow) else ""
    # 等宽字 advance ≈ 0.6em，34px → 20.4px/字符；再加 9px 让单位不贴着数字
    num_len = len(shown) * 20.4 + 9
    return f"""  <g>
    <rect x="{x}" y="{y}" width="{w}" height="100" rx="10" fill="#0e141c" stroke="#1c2733"/>
    <text class="mono" x="{x + 20}" y="{y + 27}" font-size="10" letter-spacing="2" fill="#5a6b7d">{esc(label)}</text>
    <text class="mono" x="{x + 20}" y="{y + 66}" font-size="34" font-weight="700" fill="{value_fill}"{filt}>{esc(shown)}</text>
    <text class="mono" x="{x + 20 + num_len:.1f}" y="{y + 66}" font-size="12" fill="#6b7d8f">{esc(unit)}</text>
    <rect x="{x + 20}" y="{y + 80}" width="{w - 40}" height="3" rx="1.5" fill="#1b2432"/>
    <rect x="{x + 20}" y="{y + 80}" width="{(w - 40) * bar_pct:.1f}" height="3" rx="1.5" fill="{bar_color}" opacity="{1 if value is not None else 0.25}"/>
  </g>
"""


def lang_row(x, y, name, pct, color, is_main):
    """一行语言：名字 + 分段块条 + 百分比。主力语言整行提亮。"""
    filled = max(1, round(SEGMENTS * pct / 100))
    name_fill = "#5eead4" if is_main else "#c9d1d9"
    weight = ' font-weight="700"' if is_main else ""
    parts = []
    if is_main:
        parts.append(
            f'    <rect x="{x - 8}" y="{y - 6}" width="394" height="22" rx="5" '
            f'fill="#2dd4bf" fill-opacity="0.07" stroke="#2dd4bf" stroke-opacity="0.16"/>\n'
        )
    parts.append(
        f'    <text class="mono" x="{x}" y="{y + 9}" font-size="12" fill="{name_fill}"{weight}>{esc(name.lower())}</text>\n'
    )
    if is_main:
        # 字节占比说明不了「我是干什么的」——后端代码天生比 UI 代码短，所以在这里点名主力栈
        parts.append(
            f'    <text class="mono" x="{x + 30}" y="{y + 9}" font-size="9" letter-spacing="1.2" '
            f'fill="#2dd4bf" fill-opacity="0.75">MAIN</text>\n'
        )
    lit = []
    for i in range(SEGMENTS):
        sx = x + 102 + i * 8
        if i < filled:
            lit.append(f'<rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="{color}"/>')
        else:
            parts.append(f'    <rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="#1b2432"/>\n')
    glow = f"glow{color[1:]}"
    parts.append(f'    <g filter="url(#{glow})">{"".join(lit)}</g>\n')
    parts.append(
        f'    <text class="mono" x="{x + 378}" y="{y + 9}" font-size="11.5" text-anchor="end" fill="#5a6b7d">{pct:.1f}%</text>\n'
    )
    return "".join(parts)


def build_svg(ranked, streak, ytd):
    total = sum(size for _, size in ranked) or 1
    lang_card_y = 116
    rows = -(-len(ranked) // 2)
    lang_card_h = 54 + rows * 19 + 12
    height = lang_card_y + lang_card_h

    used_colors = {LANG_COLORS.get(n, LANG_COLORS["Other"]) for n, _ in ranked}
    used_colors |= {"#2dd4bf", "#a78bfa"}
    filters = "\n".join(
        f'    <filter id="glow{c[1:]}" x="-60%" y="-160%" width="220%" height="420%">'
        f'<feDropShadow dx="0" dy="0" stdDeviation="3.2" flood-color="{c}" flood-opacity="0.55"/></filter>'
        for c in sorted(used_colors)
    )

    out = [f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}" fill="none" role="img" aria-label="grpcer activity and language stats">
  <title>grpcer — activity and most used languages</title>
  <style>
    .mono {{ font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace; }}
  </style>
  <defs>
{filters}
  </defs>
"""]

    # --- three metric cards -------------------------------------------------
    streak_pct = min(1.0, streak / 365) if streak is not None else 0.0
    ytd_pct = min(1.0, ytd / 2000) if ytd is not None else 0.0
    out.append(metric_card(0, 0, 282, "CURRENT STREAK", streak, "days", streak_pct, "#2dd4bf", "glow2dd4bf"))
    out.append(metric_card(299, 0, 282, "CONTRIBUTIONS THIS YEAR", ytd, "", ytd_pct, "#e6edf3", None,
                           bar_color="#42566d"))
    out.append(f"""  <g>
    <rect x="598" y="0" width="282" height="100" rx="10" fill="#0e141c" stroke="#1c2733"/>
    <text class="mono" x="618" y="27" font-size="10" letter-spacing="2" fill="#5a6b7d">PLATFORMS SHIPPED</text>
    <text class="mono" x="618" y="66" font-size="34" font-weight="700" fill="#a78bfa" filter="url(#glowa78bfa)">4</text>
    <text class="mono" x="644" y="66" font-size="12" fill="#6b7d8f">Go · iOS · Android · Web</text>
    <rect x="618" y="80" width="242" height="3" rx="1.5" fill="#1b2432"/>
    <rect x="618" y="80" width="242" height="3" rx="1.5" fill="#a78bfa"/>
  </g>
""")

    # --- language card ------------------------------------------------------
    out.append(f"""  <rect x="0" y="{lang_card_y}" width="{WIDTH}" height="{lang_card_h}" rx="10" fill="#0e141c" stroke="#1c2733"/>
  <text class="mono" x="22" y="{lang_card_y + 32}" font-size="10" letter-spacing="2" fill="#5a6b7d">MOST USED LANGUAGES</text>
  <text class="mono" x="858" y="{lang_card_y + 32}" font-size="10.5" text-anchor="end" fill="#3d4c5c">by code volume · incl. private repos · updated daily</text>
""")

    left, right = ranked[:rows], ranked[rows:]
    for i in range(rows):
        y = lang_card_y + 54 + i * 19
        name, size = left[i]
        out.append(lang_row(22, y, name, 100 * size / total,
                            LANG_COLORS.get(name, LANG_COLORS["Other"]), name == MAIN_STACK))
        if i < len(right):
            name, size = right[i]
            out.append(lang_row(480, y, name, 100 * size / total,
                                LANG_COLORS.get(name, LANG_COLORS["Other"]), name == MAIN_STACK))

    out.append("</svg>\n")
    return "".join(out)


def main():
    repos = list_owned_repos()
    ranked = top_n_with_other(aggregate_languages(repos))
    streak, ytd = activity_metrics(OWNER)

    os.makedirs("assets", exist_ok=True)
    with open("assets/stats.svg", "w", encoding="utf-8") as f:
        f.write(build_svg(ranked, streak, ytd))

    total = sum(s for _, s in ranked) or 1
    print("聚合仓库:", ", ".join(r["name"] for r in repos))
    print("连续提交天数:", streak, "| 今年贡献:", ytd)
    print("语言字节占比:", {n: f"{100 * s / total:.1f}%" for n, s in ranked})


if __name__ == "__main__":
    main()
