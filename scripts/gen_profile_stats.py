#!/usr/bin/env python3
"""聚合 grpcer 名下全部仓库(含 private)的活跃度与语言字节数，渲染主页用的三张 SVG。

产物：
  assets/card-*.svg    三张项目卡（oriveo / ownmem / tokpet），star 数取实时值
                       拆成独立文件是为了各自包一层 <a>——图内链接点不动
  assets/stats-*.svg     指标卡 + 语言分布
  assets/activity-*.svg  最近 26 周每日提交柱状图
每张都出 -dark / -light 两版，README 用 <picture> 按主题切换。
hero.svg 例外，只有深色一版：深色 banner 压在浅色页面上是成立的，
而它那套透视网格 + 霓虹发光换成白底就不成立了。

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


COMMITS_QUERY = """
query($login: String!, $from: DateTime!, $to: DateTime!) {
  user(login: $login) {
    contributionsCollection(from: $from, to: $to) {
      totalCommitContributions
      restrictedContributionsCount
    }
  }
}
"""


def total_commits(login, created_year):
    """从建号那年逐年累加提交数。拿不到就返回 None，由卡片渲染成"暂无"。

    用本人 token 查自己时，私有仓库的提交直接计入 totalCommitContributions，
    restrictedContributionsCount 会是 0；换成他人 token 才会拆成两半，所以两个都加。
    """
    total, got_any = 0, False
    for year in range(created_year, dt.date.today().year + 1):
        data = graphql(COMMITS_QUERY, {
            "login": login,
            "from": f"{year}-01-01T00:00:00Z",
            "to": f"{year}-12-31T23:59:59Z",
        })
        if not data or not data.get("user"):
            continue
        c = data["user"]["contributionsCollection"]
        total += c["totalCommitContributions"] + c["restrictedContributionsCount"]
        got_any = True
    return total if got_any else None


def account_created_year(login):
    data = graphql("query($login: String!) { user(login: $login) { createdAt } }", {"login": login})
    if not data or not data.get("user"):
        return dt.date.today().year
    return int(data["user"]["createdAt"][:4])


def total_stars(repos):
    return sum(r["stargazers_count"] for r in repos)


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


# ---------------------------------------------------------------- theming

THEMES = {
    "dark": {
        "card_bg": "#0e141c", "card_stroke": "#1c2733",
        "label": "#5a6b7d", "value": "#e6edf3", "body": "#8b98a5",
        "row": "#c9d1d9", "faint": "#3d4c5c", "track": "#1b2432",
        "teal": "#2dd4bf", "teal_soft": "#5eead4", "violet": "#a78bfa",
        "neutral_bar": "#42566d", "glow": 0.55, "bar_glow": 0.40,
        "bar_top": "#5eead4", "bar_mid": "#2dd4bf", "bar_bottom": "#0f766e",
        "main_fill": 0.07, "main_stroke": 0.16, "arrow": 0.55,
    },
    "light": {
        # 卡片比页面略暗（页面纯白、卡片 #f6f8fa），和 GitHub 原生卡片一个路子；
        # 深色版里是反过来的——卡片比页面亮。
        "card_bg": "#f6f8fa", "card_stroke": "#d1d9e0",
        "label": "#59636e", "value": "#1f2328", "body": "#59636e",
        "row": "#1f2328", "faint": "#818b98", "track": "#e4e8ed",
        "teal": "#0d9488", "teal_soft": "#0f766e", "violet": "#7c3aed",
        "neutral_bar": "#9aa5b1", "glow": 0.0, "bar_glow": 0.0,
        "bar_top": "#2dd4bf", "bar_mid": "#14b8a6", "bar_bottom": "#0f766e",
        "main_fill": 0.10, "main_stroke": 0.35, "arrow": 0.7,
    },
}


def tone_for_light(hex_color):
    """把过亮的 linguist 色压暗，否则在白底卡片上几乎看不见（JavaScript 的黄最明显）。"""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255
    if lum <= 0.72:
        return hex_color
    f = 0.72 / lum
    return "#%02x%02x%02x" % (int(r * f), int(g * f), int(b * f))


def lang_color(name, th):
    c = LANG_COLORS.get(name, LANG_COLORS["Other"])
    return tone_for_light(c) if th["glow"] == 0 else c


# ---------------------------------------------------------------- rendering

def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def svg_open(width, height, label):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}" fill="none" role="img" aria-label="{esc(label)}">\n'
            f'  <title>{esc(label)}</title>\n')


def card(th, x, y, w, h):
    return (f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="10" '
            f'fill="{th["card_bg"]}" stroke="{th["card_stroke"]}"/>\n')


def glow_filter(fid, color, opacity, deviation=3.2):
    if opacity <= 0:
        return ""
    return (f'    <filter id="{fid}" x="-60%" y="-160%" width="220%" height="420%">'
            f'<feDropShadow dx="0" dy="0" stdDeviation="{deviation}" flood-color="{color}" '
            f'flood-opacity="{opacity}"/></filter>\n')


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


def build_card_svg(th, icon, tag, name, lines, footer, accent, *, trailing_gap):
    """一张项目卡。trailing_gap=True 时右侧留出卡间距，好让三张紧挨着排也不粘连。"""
    width = CARD_W + (CARD_GAP if trailing_gap else 0)
    out = [svg_open(width, CARD_H, f'{name} — {" ".join(lines)}')]
    out.append(card(th, 0, 0, CARD_W, CARD_H))
    out.append(f'  <g transform="translate({CARD_W - 44}, 20)" stroke="{accent}" stroke-opacity="0.5" '
               f'stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round">'
               f'{CARD_ICONS[icon]}</g>\n')
    out.append(text(20, 32, tag, 10, accent, spacing=2, opacity="0.9"))
    out.append(text(20, 62, name, 21, th["value"], weight="700"))
    for i, line in enumerate(lines):
        out.append(text(20, 88 + i * 17, line, 11.5, th["body"]))
    out.append(text(20, CARD_H - 20, footer, 11, accent, opacity="0.85"))
    # 右下角箭头：图片本身没有 hover 态，用它暗示这张卡是可以点的
    out.append(f'  <path d="M{CARD_W - 34} {CARD_H - 24}h10m-4-4l4 4-4 4" stroke="{accent}" '
               f'stroke-opacity="{th["arrow"]}" stroke-width="1.5" fill="none" '
               f'stroke-linecap="round" stroke-linejoin="round"/>\n')
    out.append("</svg>\n")
    return "".join(out)


def build_all_cards(th, suffix, ownmem_stars, tokpet_stars):
    def stars(n, tail):
        return f"\u2605 {n} \u00b7 {tail}" if n is not None else tail
    return {
        f"assets/card-oriveo-{suffix}.svg": build_card_svg(
            th, "cube", "FOUNDER", "oriveo",
            ["BYOK multi-model AI client.", "15 providers + custom relay."],
            "iOS \u00b7 Android \u00b7 Web \u00b7 Go", th["teal"], trailing_gap=True),
        f"assets/card-ownmem-{suffix}.svg": build_card_svg(
            th, "book", "OPEN SOURCE", "ownmem",
            ["Git-native memory for AI", "coding agents. On npm."],
            stars(ownmem_stars, "Apache-2.0"), th["violet"], trailing_gap=True),
        f"assets/card-tokpet-{suffix}.svg": build_card_svg(
            th, "pet", "OPEN SOURCE", "tokpet",
            ["Desktop pet that watches", "your AI token spend."],
            stars(tokpet_stars, "brew install"), th["teal"], trailing_gap=False),
    }


# ---- stats ---------------------------------------------------------------

def metric_card(th, x, label, value, unit, bar_pct, color, glow, bar_color=None):
    """小标签 + 大数字 + 一条细进度线。value 为 None 时诚实显示"暂无"。"""
    w = 278
    shown = "\u2014" if value is None else f"{value:,}"
    if value is None:
        unit = "no data yet"
    bar_color = bar_color or color
    out = [card(th, x, 0, w, 100)]
    out.append(text(x + 20, 27, label, 10, th["label"], spacing=2))
    out.append(text(x + 20, 66, shown, 34, color if value is not None else th["faint"],
                    weight="700", filt=glow if (value is not None and glow) else None))
    # 等宽字 advance ≈ 0.6em，34px → 20.4px/字符；再加 9px 让单位不贴着数字
    out.append(text(round(x + 20 + len(shown) * 20.4 + 9, 1), 66, unit, 12, th["body"]))
    out.append(f'  <rect x="{x + 20}" y="80" width="{w - 40}" height="3" rx="1.5" fill="{th["track"]}"/>\n')
    out.append(f'  <rect x="{x + 20}" y="80" width="{(w - 40) * bar_pct:.1f}" height="3" rx="1.5" '
               f'fill="{bar_color}" opacity="{1 if value is not None else 0.25}"/>\n')
    return "".join(out)


def lang_row(th, x, y, name, pct, is_main):
    """名字 + 分段块条 + 百分比。主力语言整行提亮——字节占比说不了"我是干什么的"。"""
    color = lang_color(name, th)
    filled = max(1, round(SEGMENTS * pct / 100))
    out = []
    if is_main:
        out.append(f'  <rect x="{x - 8}" y="{y - 6}" width="394" height="22" rx="5" '
                   f'fill="{th["teal"]}" fill-opacity="{th["main_fill"]}" '
                   f'stroke="{th["teal"]}" stroke-opacity="{th["main_stroke"]}"/>\n')
    out.append(text(x, y + 9, name.lower(), 12, th["teal_soft"] if is_main else th["row"],
                    weight="700" if is_main else None))
    if is_main:
        out.append(text(x + 30, y + 9, "MAIN", 9, th["teal"], spacing=1.2, opacity="0.75"))
    lit = []
    for i in range(SEGMENTS):
        sx = x + 102 + i * 8
        if i < filled:
            lit.append(f'<rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="{color}"/>')
        else:
            out.append(f'  <rect x="{sx}" y="{y}" width="6" height="10" rx="1" fill="{th["track"]}"/>\n')
    cells = "".join(lit)
    out.append(f'  <g filter="url(#glow{color[1:]})">{cells}</g>\n' if th["glow"] > 0
               else f'  <g>{cells}</g>\n')
    out.append(text(x + 378, y + 9, f"{pct:.1f}%", 11.5, th["label"], anchor="end"))
    return "".join(out)


def build_stats_svg(th, ranked, streak, commits, stars):
    total = sum(size for _, size in ranked) or 1
    lang_y = 116
    rows = -(-len(ranked) // 2)
    lang_h = 54 + rows * 19 + 12
    height = lang_y + lang_h

    out = [svg_open(WIDTH, height, "grpcer — commits, stars, streak and most used languages")]
    out.append("  <defs>\n")
    for c in sorted({lang_color(n, th) for n, _ in ranked} | {th["teal"], th["violet"]}):
        out.append(glow_filter(f"glow{c[1:]}", c, th["glow"]))
    out.append("  </defs>\n")

    tealglow = f'glow{th["teal"][1:]}' if th["glow"] > 0 else None
    violetglow = f'glow{th["violet"][1:]}' if th["glow"] > 0 else None
    out.append(metric_card(th, 0, "TOTAL COMMITS", commits, "all time",
                           min(1.0, commits / 10000) if commits is not None else 0.0,
                           th["teal"], tealglow))
    out.append(metric_card(th, 296, "TOTAL STARS", stars, "across all repos",
                           min(1.0, stars / 1000) if stars is not None else 0.0,
                           th["violet"], violetglow))
    out.append(metric_card(th, 592, "CURRENT STREAK", streak, "days",
                           # 按月映射：9/365 画出来只有 2.5%，看着像渲染坏了
                           min(1.0, streak / 30) if streak is not None else 0.0,
                           th["value"], None, bar_color=th["neutral_bar"]))

    out.append(card(th, 0, lang_y, WIDTH, lang_h))
    out.append(text(22, lang_y + 32, "MOST USED LANGUAGES", 10, th["label"], spacing=2))
    out.append(text(848, lang_y + 32, "by code volume \u00b7 incl. private repos \u00b7 updated daily",
                    10.5, th["faint"], anchor="end"))
    left, right = ranked[:rows], ranked[rows:]
    for i in range(rows):
        y = lang_y + 54 + i * 19
        n, sz = left[i]
        out.append(lang_row(th, 22, y, n, 100 * sz / total, n == MAIN_STACK))
        if i < len(right):
            n, sz = right[i]
            out.append(lang_row(th, 470, y, n, 100 * sz / total, n == MAIN_STACK))
    out.append("</svg>\n")
    return "".join(out)


# ---- activity ------------------------------------------------------------

def build_activity_svg(th, days):
    """最近 26 周的每日提交柱状图。没有数据时画一张明说"暂无"的空卡，不画假柱子。"""
    pad, bar_top, bar_h = 22, 54, 96
    height = bar_top + bar_h + 40
    out = [svg_open(WIDTH, height, "grpcer — daily commits over the last 26 weeks")]
    out.append(f'  <defs>\n'
               f'    <linearGradient id="barGrad" x1="0" y1="{bar_top}" x2="0" '
               f'y2="{bar_top + bar_h}" gradientUnits="userSpaceOnUse">\n'
               f'      <stop offset="0" stop-color="{th["bar_top"]}"/>\n'
               f'      <stop offset="0.55" stop-color="{th["bar_mid"]}"/>\n'
               f'      <stop offset="1" stop-color="{th["bar_bottom"]}"/>\n'
               f'    </linearGradient>\n')
    out.append(glow_filter("barGlow", th["teal"], th["bar_glow"], deviation=2.4))
    out.append('  </defs>\n')
    out.append(card(th, 0, 0, WIDTH, height))
    out.append(text(pad, 32, "DAILY COMMITS", 10, th["label"], spacing=2))

    if not days:
        out.append(text(WIDTH - pad, 32, "no data yet", 10.5, th["faint"], anchor="end"))
        out.append(text(WIDTH / 2, bar_top + bar_h / 2, "contribution data unavailable",
                        12, th["faint"], anchor="middle"))
        out.append("</svg>\n")
        return "".join(out)

    today = dt.date.today()
    window = [today - dt.timedelta(days=ACTIVITY_DAYS - 1 - i) for i in range(ACTIVITY_DAYS)]
    counts = [days.get(d.isoformat(), 0) for d in window]
    peak = max(counts) or 1
    step = (WIDTH - 2 * pad) / ACTIVITY_DAYS
    bw = round(step - 1.1, 2)

    out.append(text(WIDTH - pad, 32, f"last 26 weeks \u00b7 peak {peak} on one day",
                    10.5, th["faint"], anchor="end"))

    bars, zeros = [], []
    for i, c in enumerate(counts):
        x = round(pad + i * step, 2)
        if c == 0:
            # 零贡献那天画成 2px 的墩子：空白会被误读成"没数据"，这里是"那天真的是 0"
            zeros.append(f'<rect x="{x}" y="{bar_top + bar_h - 2}" width="{bw}" height="2" '
                         f'rx="1" fill="{th["track"]}"/>')
            continue
        hgt = max(3.0, round(bar_h * c / peak, 2))
        bars.append(f'<rect x="{x}" y="{round(bar_top + bar_h - hgt, 2)}" width="{bw}" '
                    f'height="{hgt}" rx="1.2" fill="url(#barGrad)"/>')
    out.append(f'  <g>{"".join(zeros)}</g>\n')
    out.append(f'  <g filter="url(#barGlow)">{"".join(bars)}</g>\n' if th["bar_glow"] > 0
               else f'  <g>{"".join(bars)}</g>\n')
    out.append(f'  <rect x="{pad}" y="{bar_top + bar_h}" width="{WIDTH - 2 * pad}" height="1" '
               f'fill="{th["card_stroke"]}"/>\n')

    # 月份刻度：每个月 1 号落在窗口里就标一次
    seen = set()
    for i, d in enumerate(window):
        if d.day == 1 and d.month not in seen:
            seen.add(d.month)
            out.append(text(round(pad + i * step, 2), bar_top + bar_h + 20,
                            d.strftime("%b"), 10, th["faint"]))
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
    commits = total_commits(OWNER, account_created_year(OWNER))
    stars = total_stars(repos)
    om, tp = repo_stars("ownmem"), repo_stars("tokpet")

    os.makedirs("assets", exist_ok=True)
    written = []
    for suffix, th in (("dark", THEMES["dark"]), ("light", THEMES["light"])):
        products = build_all_cards(th, suffix, om, tp)
        products[f"assets/stats-{suffix}.svg"] = build_stats_svg(th, ranked, streak, commits, stars)
        products[f"assets/activity-{suffix}.svg"] = build_activity_svg(th, days)
        for path, svg in products.items():
            with open(path, "w", encoding="utf-8") as f:
                f.write(svg)
            written.append(path)
    print("写出:", len(written), "个文件")

    total = sum(s for _, s in ranked) or 1
    print("聚合仓库:", ", ".join(r["name"] for r in repos))
    print("累计提交:", commits, "| 总 star:", stars,
          "| 连续天数:", streak, "| 今年贡献:", ytd, "| 日历天数:", len(days))
    print("语言字节占比:", {n: f"{100 * s / total:.1f}%" for n, s in ranked})


if __name__ == "__main__":
    main()
