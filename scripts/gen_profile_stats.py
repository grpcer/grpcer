#!/usr/bin/env python3
"""聚合 grpcer 名下全部仓库(含 private)的活跃度与语言字节数，渲染主页用的三张 SVG。

产物：
  assets/card-*.svg    三张项目卡（oriveo / ownmem / tokpet），star 数取实时值
                       拆成独立文件是为了各自包一层 <a>——图内链接点不动
  assets/stats.svg     指标卡 + 语言分布
  assets/activity.svg  最近 26 周每日提交柱状图

私有仓库的源码内容不会被写入产物，只有聚合后的计数与语言字节占比会出现在生成的 SVG 里。
语言口径是 GitHub linguist 的**字节数**——UI 代码天然比后端代码体积大，所以卡片标题写明了口径。
拿不到的指标一律渲染成"暂无"，不拿 0 冒充真实值。
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

WIDTH = 870          # README 容器约 878，留 8px 余量：并排的卡片超一点就换行
SEGMENTS = 28
ACTIVITY_DAYS = 182          # 26 周：880px 下每根柱子还有 4.6px，再长就糊成一片了

CARD_BG = "#0e141c"
CARD_STROKE = "#1c2733"
LABEL = "#5a6b7d"
TEAL = "#2dd4bf"
VIOLET = "#a78bfa"

MONO = ('font-family="JetBrains Mono, ui-monospace, SFMono-Regular, '
        'Menlo, Consolas, Liberation Mono, monospace"')


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
    """契约数据拿不到时返回 None，由调用方降级成"暂无"，不要中断整张图的渲染。"""
    body = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(
        API + "/graphql",
        data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
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
    """返回 (连续提交天数, 今年贡献数, {日期: 次数})；拿不到就是 (None, None, {})。"""
    today = dt.date.today()
    # contributionsCollection 一次最多查一年，连续天数可能跨年，所以查两段再合并
    recent = fetch_contribution_days(login, today - dt.timedelta(days=364), today)
    if recent is None:
        return None, None, {}
    days = recent[1]
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
    return streak, ytd_total, days


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


def svg_open(height, label):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
            f'viewBox="0 0 {WIDTH} {height}" fill="none" role="img" aria-label="{esc(label)}">\n'
            f'  <title>{esc(label)}</title>\n')


def card(x, y, w, h):
    return (f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
            f'fill="{CARD_BG}" stroke="{CARD_STROKE}"/>\n')


def text(x, y, s, size, fill, *, weight=None, anchor=None, spacing=None,
         opacity=None, filt=None, length=None):
    a = [f'x="{x}"', f'y="{y}"', MONO, f'font-size="{size}"', f'fill="{fill}"']
    if weight:  a.append(f'font-weight="{weight}"')
    if anchor:  a.append(f'text-anchor="{anchor}"')
    if spacing: a.append(f'letter-spacing="{spacing}"')
    if opacity: a.append(f'fill-opacity="{opacity}"')
    if filt:    a.append(f'filter="url(#{filt})"')
    if length:  a.append(f'textLength="{length}" lengthAdjust="spacingAndGlyphs"')
    return f'  <text {" ".join(a)}>{esc(s)}</text>\n'


# ---- project cards ------------------------------------------------------

# 每张卡是独立 SVG：SVG 当 <img> 加载时图内 <a> 不可点，只有把卡拆开、
# 各自包一层 markdown 的 <a>，点击才能跳到对应仓库。
CARD_ICONS = {
    "cube": '<path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3z"/><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5"/>',
    "book": '<path d="M4 5.5A2.5 2.5 0 016.5 3H19v15H6.5A2.5 2.5 0 004 20.5z"/><path d="M4 20.5A2.5 2.5 0 016.5 18H19v3H6.5A2.5 2.5 0 014 20.5z"/><path d="M9 8h6"/>',
    "pet":  '<rect x="3" y="4" width="18" height="14" rx="2"/><path d="M8 21h8M12 18v3"/><path d="M8.5 9.5h.01M15.5 9.5h.01"/><path d="M9 13c1.6 1.3 4.4 1.3 6 0"/>',
}

CARD_W, CARD_H, CARD_GAP = 278, 148, 18   # 278*3 + 18*2 = 870


def build_card_svg(icon, tag, name, lines, footer, color, *, trailing_gap):
    """一张项目卡。trailing_gap=True 时右侧留出卡间距，好让三张紧挨着排也不粘连。"""
    width = CARD_W + (CARD_GAP if trailing_gap else 0)
    out = [f'<?xml version="1.0" encoding="UTF-8"?>\n'
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{CARD_H}" '
           f'viewBox="0 0 {width} {CARD_H}" fill="none" role="img" '
           f'aria-label="{esc(name)} — {esc(" ".join(lines))}">\n'
           f'  <title>{esc(name)}</title>\n']
    out.append(card(0, 0, CARD_W, CARD_H))
    out.append(f'  <g transform="translate({CARD_W - 44}, 20)" stroke="{color}" stroke-opacity="0.5" '
               f'stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round">'
               f'{CARD_ICONS[icon]}</g>\n')
    out.append(text(20, 32, tag, 10, color, spacing=2, opacity="0.9"))
    out.append(text(20, 62, name, 21, "#e6edf3", weight="700"))
    for i, line in enumerate(lines):
        out.append(text(20, 88 + i * 17, line, 11.5, "#8b98a5"))
    out.append(text(20, CARD_H - 20, footer, 11, color, opacity="0.85"))
    # 右下角箭头：图片本身没有 hover 态，用它暗示这张卡是可以点的
    out.append(f'  <path d="M{CARD_W - 34} {CARD_H - 24}h10m-4-4l4 4-4 4" stroke="{color}" '
               f'stroke-opacity="0.55" stroke-width="1.5" fill="none" '
               f'stroke-linecap="round" stroke-linejoin="round"/>\n')
    out.append("</svg>\n")
    return "".join(out)


def build_all_cards(ownmem_stars, tokpet_stars):
    def stars(n, suffix):
        return f"★ {n} · {suffix}" if n is not None else suffix
    return {
        "assets/card-oriveo.svg": build_card_svg(
            "cube", "FOUNDER", "oriveo",
            ["BYOK multi-model AI client.", "15 providers + custom relay."],
            "iOS · Android · Web · Go", TEAL, trailing_gap=True),
        "assets/card-ownmem.svg": build_card_svg(
            "book", "OPEN SOURCE", "ownmem",
            ["Git-native memory for AI", "coding agents. On npm."],
            stars(ownmem_stars, "Apache-2.0"), VIOLET, trailing_gap=True),
        "assets/card-tokpet.svg": build_card_svg(
            "pet", "OPEN SOURCE", "tokpet",
            ["Desktop pet that watches", "your AI token spend."],
            stars(tokpet_stars, "brew install"), TEAL, trailing_gap=False),
    }


# ---- stats ---------------------------------------------------------------

def metric_card(x, label, value, unit, bar_pct, color, glow, bar_color=None):
    """小标签 + 大数字 + 一条细进度线。value 为 None 时诚实显示"暂无"。"""
    w = 278
    shown = "—" if value is None else f"{value:,}"
    if value is None:
        unit = "no data yet"
    bar_color = bar_color or color
    out = [card(x, 0, w, 100)]
    out.append(text(x + 20, 27, label, 10, LABEL, spacing=2))
    out.append(text(x + 20, 66, shown, 34, color if value is not None else "#3d4c5c",
                    weight="700", filt=glow if (value is not None and glow) else None))
    # 等宽字 advance ≈ 0.6em，34px → 20.4px/字符；再加 9px 让单位不贴着数字
    out.append(text(round(x + 20 + len(shown) * 20.4 + 9, 1), 66, unit, 12, "#6b7d8f"))
    out.append(f'  <rect x="{x + 20}" y="80" width="{w - 40}" height="3" rx="1.5" fill="#1b2432"/>\n')
    out.append(f'  <rect x="{x + 20}" y="80" width="{(w - 40) * bar_pct:.1f}" height="3" rx="1.5" '
               f'fill="{bar_color}" opacity="{1 if value is not None else 0.25}"/>\n')
    return "".join(out)


def lang_row(x, y, name, pct, color, is_main):
    """名字 + 分段块条 + 百分比。主力语言整行提亮——字节占比说不了"我是干什么的"。"""
    filled = max(1, round(SEGMENTS * pct / 100))
    out = []
    if is_main:
        out.append(f'  <rect x="{x - 8}" y="{y - 6}" width="394" height="22" rx="5" '
                   f'fill="{TEAL}" fill-opacity="0.07" stroke="{TEAL}" stroke-opacity="0.16"/>\n')
    out.append(text(x, y + 9, name.lower(), 12, "#5eead4" if is_main else "#c9d1d9",
                    weight="700" if is_main else None))
    if is_main:
        out.append(text(x + 30, y + 9, "MAIN", 9, TEAL, spacing=1.2, opacity="0.75"))
    lit = []
    for i in range(SEGMENTS):
        sx = x + 102 + i * 8
        if i < filled:
            lit.append(f'<rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="{color}"/>')
        else:
            out.append(f'  <rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="#1b2432"/>\n')
    out.append(f'  <g filter="url(#glow{color[1:]})">{"".join(lit)}</g>\n')
    out.append(text(x + 378, y + 9, f"{pct:.1f}%", 11.5, LABEL, anchor="end"))
    return "".join(out)


def build_stats_svg(ranked, streak, ytd):
    total = sum(size for _, size in ranked) or 1
    lang_y = 116
    rows = -(-len(ranked) // 2)
    lang_h = 54 + rows * 19 + 12
    height = lang_y + lang_h

    colors = {LANG_COLORS.get(n, LANG_COLORS["Other"]) for n, _ in ranked} | {TEAL, VIOLET}
    out = [svg_open(height, "grpcer — activity and most used languages")]
    out.append("  <defs>\n")
    for c in sorted(colors):
        out.append(f'    <filter id="glow{c[1:]}" x="-60%" y="-160%" width="220%" height="420%">'
                   f'<feDropShadow dx="0" dy="0" stdDeviation="3.2" flood-color="{c}" '
                   f'flood-opacity="0.55"/></filter>\n')
    out.append("  </defs>\n")

    out.append(metric_card(0, "CURRENT STREAK", streak, "days",
                           # 按月映射：9/365 画出来只有 2.5%，看着像渲染坏了
                           min(1.0, streak / 30) if streak is not None else 0.0,
                           TEAL, f"glow{TEAL[1:]}"))
    out.append(metric_card(296, "CONTRIBUTIONS THIS YEAR", ytd, "",
                           min(1.0, ytd / 8000) if ytd is not None else 0.0,
                           "#e6edf3", None, bar_color="#42566d"))
    out.append(metric_card(592, "PLATFORMS SHIPPED", 4, "Go · iOS · Android · Web",
                           1.0, VIOLET, f"glow{VIOLET[1:]}"))

    out.append(card(0, lang_y, WIDTH, lang_h))
    out.append(text(22, lang_y + 32, "MOST USED LANGUAGES", 10, LABEL, spacing=2))
    out.append(text(848, lang_y + 32, "by code volume · incl. private repos · updated daily",
                    10.5, "#3d4c5c", anchor="end"))
    left, right = ranked[:rows], ranked[rows:]
    for i in range(rows):
        y = lang_y + 54 + i * 19
        n, s = left[i]
        out.append(lang_row(22, y, n, 100 * s / total,
                            LANG_COLORS.get(n, LANG_COLORS["Other"]), n == MAIN_STACK))
        if i < len(right):
            n, s = right[i]
            out.append(lang_row(470, y, n, 100 * s / total,
                                LANG_COLORS.get(n, LANG_COLORS["Other"]), n == MAIN_STACK))
    out.append("</svg>\n")
    return "".join(out)


# ---- activity ------------------------------------------------------------

def build_activity_svg(days):
    """最近 26 周的每日提交柱状图。没有数据时画一张明说"暂无"的空卡，不画假柱子。"""
    pad, bar_top, bar_h = 22, 54, 96
    height = bar_top + bar_h + 40
    out = [svg_open(height, "grpcer — daily commits over the last 26 weeks")]
    out.append(f'  <defs>\n'
               f'    <linearGradient id="barGrad" x1="0" y1="{bar_top}" x2="0" '
               f'y2="{bar_top + bar_h}" gradientUnits="userSpaceOnUse">\n'
               f'      <stop offset="0" stop-color="#5eead4"/>\n'
               f'      <stop offset="0.55" stop-color="{TEAL}"/>\n'
               f'      <stop offset="1" stop-color="#0f766e"/>\n'
               f'    </linearGradient>\n'
               f'    <filter id="barGlow" x="-30%" y="-30%" width="160%" height="160%">'
               f'<feDropShadow dx="0" dy="0" stdDeviation="2.4" flood-color="{TEAL}" '
               f'flood-opacity="0.4"/></filter>\n'
               f'  </defs>\n')
    out.append(card(0, 0, WIDTH, height))
    out.append(text(pad, 32, "DAILY COMMITS", 10, LABEL, spacing=2))

    if not days:
        out.append(text(WIDTH - pad, 32, "no data yet", 10.5, "#3d4c5c", anchor="end"))
        out.append(text(WIDTH / 2, bar_top + bar_h / 2, "contribution data unavailable",
                        12, "#3d4c5c", anchor="middle"))
        out.append("</svg>\n")
        return "".join(out)

    today = dt.date.today()
    window = [today - dt.timedelta(days=ACTIVITY_DAYS - 1 - i) for i in range(ACTIVITY_DAYS)]
    counts = [days.get(d.isoformat(), 0) for d in window]
    peak = max(counts) or 1
    step = (WIDTH - 2 * pad) / ACTIVITY_DAYS
    bw = round(step - 1.1, 2)

    out.append(text(WIDTH - pad, 32,
                    f"last 26 weeks · peak {peak} on one day", 10.5, "#3d4c5c", anchor="end"))

    bars, zeros = [], []
    for i, c in enumerate(counts):
        x = round(pad + i * step, 2)
        if c == 0:
            # 零贡献那天画成 2px 的墩子：空白会被误读成"没数据"，这里是"那天真的是 0"
            zeros.append(f'<rect x="{x}" y="{bar_top + bar_h - 2}" width="{bw}" height="2" '
                         f'rx="1" fill="#1b2432"/>')
            continue
        hgt = max(3.0, round(bar_h * c / peak, 2))
        bars.append(f'<rect x="{x}" y="{round(bar_top + bar_h - hgt, 2)}" width="{bw}" '
                    f'height="{hgt}" rx="1.2" fill="url(#barGrad)"/>')
    out.append(f'  <g>{"".join(zeros)}</g>\n')
    out.append(f'  <g filter="url(#barGlow)">{"".join(bars)}</g>\n')
    out.append(f'  <rect x="{pad}" y="{bar_top + bar_h}" width="{WIDTH - 2 * pad}" height="1" '
               f'fill="#1c2733"/>\n')

    # 月份刻度：每个月 1 号落在窗口里就标一次
    seen = set()
    for i, d in enumerate(window):
        if d.day == 1 and d.month not in seen:
            seen.add(d.month)
            out.append(text(round(pad + i * step, 2), bar_top + bar_h + 20,
                            d.strftime("%b"), 10, "#3d4c5c"))
    out.append("</svg>\n")
    return "".join(out)


# ---------------------------------------------------------------- main

def repo_stars(name):
    try:
        return rest_get(f"/repos/{OWNER}/{name}")["stargazers_count"]
    except SystemExit as e:
        print(f"[warn] 取 {name} star 数失败，卡片降级为不显示星数: {e}")
        return None


def main():
    repos = list_owned_repos()
    ranked = top_n_with_other(aggregate_languages(repos))
    streak, ytd, days = activity_metrics(OWNER)

    os.makedirs("assets", exist_ok=True)
    products = build_all_cards(repo_stars("ownmem"), repo_stars("tokpet"))
    products["assets/stats.svg"] = build_stats_svg(ranked, streak, ytd)
    products["assets/activity.svg"] = build_activity_svg(days)
    for path, svg in products.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(svg)
        print("写出:", path)

    total = sum(s for _, s in ranked) or 1
    print("聚合仓库:", ", ".join(r["name"] for r in repos))
    print("连续提交天数:", streak, "| 今年贡献:", ytd, "| 日历天数:", len(days))
    print("语言字节占比:", {n: f"{100 * s / total:.1f}%" for n, s in ranked})


if __name__ == "__main__":
    main()
