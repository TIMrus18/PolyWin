import time
import json
import re
import os
import sys
import asyncio
import requests
import logging
import threading
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from rapidfuzz import fuzz

# ==========================================
# 1. ЗАГРУЗКА НАСТРОЕК И ЛОГИРОВАНИЕ
# ==========================================
load_dotenv()


def setup_logging():
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', '%H:%M:%S')

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)

    file = RotatingFileHandler('scanner.log', maxBytes=5_000_000, backupCount=3, encoding='utf-8')
    file.setLevel(logging.DEBUG)
    file.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers = [console, file]

    for noisy in ("urllib3", "asyncio", "charset_normalizer", "playwright"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


setup_logging()

CONFIG = {
    "bankroll": float(os.getenv("BANKROLL", 100.0)),
    "min_roi": float(os.getenv("MIN_ROI", 1.0)),
    "cooldown": int(os.getenv("COOLDOWN", 300)),
    "match_threshold": int(os.getenv("MATCH_THRESHOLD", 82)),
    "poly_fee_rate": float(os.getenv("POLY_FEE_RATE", 0.05)),
    "miniapp_url": os.getenv("MINIAPP_URL", ""),
    "tg_token": os.getenv("TG_TOKEN"),
    "tg_chat_id": os.getenv("TG_CHAT_ID"),
    "headless": False,
    "pw_proxy": {
        "server": os.getenv("PW_PROXY_SERVER"),
        "username": os.getenv("PW_PROXY_USER"),
        "password": os.getenv("PW_PROXY_PASS")
    } if os.getenv("PW_PROXY_SERVER") else None
}

PROXY_URL = os.getenv("PROXY_URL")
PROXY_REQUESTS = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else {}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(HEADERS)
if PROXY_REQUESTS:
    session.proxies.update(PROXY_REQUESTS)

BOT_STATE = {"paused": False, "last_update_id": 0, "start_time": time.time()}
ACTIVE_ARBS = {}
MARKERS = {"academy", "youth", "junior", "female", "women", "fe", "ii", "2", "gc"}
POLY_CACHE = {"events": [], "last_update": 0, "last_fail": 0}

# === WINLINE URLS ===
WINLINE_URLS = {
    "wl_dota2": "https://winline.ru/stavki/sport/kibersport/dota_2",
    "wl_cs": "https://winline.ru/stavki/sport/kibersport/counter-strike",
    "wl_lol": "https://winline.ru/stavki/sport/kibersport/league_of_legends",
    "wl_live": "https://winline.ru/stavki/sport/kibersport/live"
}

# === 1WIN URLS ===
ONEWIN_BASE = os.getenv("ONEWIN_DOMAIN", "https://1win.run").rstrip("/")
ONEWIN_URLS = {
    "1w_cs": f"{ONEWIN_BASE}/betting/esport/cs2-142?sub1=18eruc1.5.gpoji2&sub3=id1014",
    "1w_dota2": f"{ONEWIN_BASE}/betting/esport/dota-2-47?sub1=18eruc1.5.gpoji2&sub3=id1014",
    "1w_lol": f"{ONEWIN_BASE}/betting/esport/lol-37?sub1=18eruc1.5.gpoji2&sub3=id1014",
    "1w_live": f"{ONEWIN_BASE}/betting/esport?sub1=18eruc1.5.gpoji2&sub3=id1014"
}

# === CSGOPOSITIVE URLS ===
POSITIVE_URLS = {
    "pos_all": os.getenv("POSITIVE_URL", "https://csgopositive.com/en/")
}


# ==========================================
# 2. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ДЛЯ ТЕКСТА
# ==========================================
def norm(name):
    return re.sub(r'\s+', ' ', re.sub(r'\b(team|esports|gaming|club)\b', '', name.lower())).strip()


def get_markers(name):
    return {t for t in re.findall(r'\w+', name.lower()) if t in MARKERS}


def split_title(title):
    clean = re.sub(r'^[^:]+:\s*', '', title)
    parts = re.split(r'\s+vs\.?\s+', clean, maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    return parts[0].strip(), parts[1].split(" (")[0].split(" -")[0].strip()


def short_forms(name):
    letters = re.sub(r'[^A-Za-z0-9 ]', '', name)
    forms = {
        "".join(c for c in letters if c.isupper() or c.isdigit()).lower(),
        "".join(w[0] for w in letters.split()).lower()
    }
    return {f for f in forms if len(f) >= 2}


def outcome_score(outcome, team):
    o, t = norm(outcome), norm(team)
    if not o or not t:
        return 0
    s = fuzz.token_set_ratio(o, t)
    if o.replace(" ", "") in short_forms(team):
        s = max(s, 95)
    return s


def resolve_outcomes(outcomes, tokens, t1, t2):
    direct = min(outcome_score(outcomes[0], t1), outcome_score(outcomes[1], t2))
    swapped = min(outcome_score(outcomes[0], t2), outcome_score(outcomes[1], t1))
    if max(direct, swapped) < 70:
        return None
    return tokens if direct >= swapped else [tokens[1], tokens[0]]


# ==========================================
# 3. TELEGRAM (КАЛЬКУЛЯТОР И УВЕДОМЛЕНИЯ)
# ==========================================
def send_tg_alert(text, calc_params=None):
    if not CONFIG["tg_token"] or CONFIG["tg_token"] == "ВАШ_НОВЫЙ_ТОКЕН_БОТА":
        return
    url = f"https://api.telegram.org/bot{CONFIG['tg_token']}/sendMessage"
    payload = {
        "chat_id": CONFIG["tg_chat_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True
    }

    if calc_params and CONFIG["miniapp_url"]:
        payload["reply_markup"] = {
            "inline_keyboard": [[{
                "text": "🧮 Открыть в калькуляторе",
                "web_app": {"url": f"{CONFIG['miniapp_url']}?{calc_params}"}
            }]]
        }

    try:
        with requests.Session() as s:
            s.headers.update(HEADERS)
            if PROXY_REQUESTS:
                s.proxies.update(PROXY_REQUESTS)
            s.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.warning(f"Ошибка TG: {e}")


def handle_calc_command(text):
    parts = text.split()
    try:
        if len(parts) == 3:
            bank = CONFIG["bankroll"]
            odd1 = float(parts[1].replace(',', '.'))
            odd2 = float(parts[2].replace(',', '.'))
        elif len(parts) == 4:
            bank = float(parts[1].replace(',', '.'))
            odd1 = float(parts[2].replace(',', '.'))
            odd2 = float(parts[3].replace(',', '.'))
        else:
            send_tg_alert(
                "❌ <b>Ошибка формата</b>\nИспользуйте:\n/calc <кэф1> <кэф2>\nили\n/calc <сумма> <кэф1> <кэф2>")
            return
    except ValueError:
        send_tg_alert("❌ Ошибка: кэфы и сумма должны быть числами.")
        return

    if odd1 <= 1.0 or odd2 <= 1.0:
        send_tg_alert("❌ Ошибка: коэффициенты должны быть больше 1.0")
        return

    margin = (1.0 / odd1) + (1.0 / odd2)
    if margin >= 1.0:
        roi = ((1.0 / margin) - 1.0) * 100
        send_tg_alert(f"⚠️ <b>Это не вилка</b>\nКэфы: {odd1} и {odd2}\nМаржа: {margin:.4f}\nROI: {roi:.2f}% (убыток)")
        return

    stake1 = bank / (odd1 * margin)
    stake2 = bank / (odd2 * margin)
    profit = (stake1 * odd1) - bank
    roi = ((1.0 / margin) - 1.0) * 100

    res_text = (f"✅ <b>Расчет вилки</b>\nБанк: <b>{bank:.2f}$</b>\nКэф 1: {odd1} | Кэф 2: {odd2}\n"
                f"Маржа: {margin:.4f} | ROI: <b>+{roi:.2f}%</b>\n"
                f"👉 <b>Ставка 1:</b> {stake1:.2f}$ (выплата {stake1 * odd1:.2f}$)\n"
                f"👉 <b>Ставка 2:</b> {stake2:.2f}$ (выплата {stake2 * odd2:.2f}$)\n"
                f"💰 <b>Чистый профит: +{profit:.2f}$</b>")
    send_tg_alert(res_text)


def tg_bot_thread():
    if not CONFIG["tg_token"] or CONFIG["tg_token"] == "ВАШ_НОВЫЙ_ТОКЕН_БОТА":
        return

    logging.info("🤖 Поток Telegram-бота запущен.")
    tg_session = requests.Session()
    tg_session.headers.update(HEADERS)
    if PROXY_REQUESTS:
        tg_session.proxies.update(PROXY_REQUESTS)

    while True:
        try:
            res = tg_session.get(
                f"https://api.telegram.org/bot{CONFIG['tg_token']}/getUpdates",
                params={"offset": BOT_STATE["last_update_id"] + 1, "timeout": 10},
                timeout=15
            ).json()
            if res.get("ok"):
                for up in res["result"]:
                    BOT_STATE["last_update_id"] = up["update_id"]
                    msg = up.get("message", {})
                    text = msg.get("text", "").strip()
                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    if chat_id != str(CONFIG["tg_chat_id"]):
                        continue

                    if text == "/pause":
                        BOT_STATE["paused"] = True
                        send_tg_alert("⏸ <b>Мониторинг приостановлен.</b>")
                    elif text == "/resume":
                        BOT_STATE["paused"] = False
                        send_tg_alert("▶️ <b>Мониторинг возобновлен.</b>")
                    elif text == "/status":
                        uptime = int(time.time() - BOT_STATE["start_time"])
                        st = "⏸ На паузе" if BOT_STATE["paused"] else "▶️ Работает"
                        send_tg_alert(
                            f"📊 <b>Статус:</b> {st}\n⏱ Аптайм: {uptime} сек.\n🔥 Мин ROI: {CONFIG['min_roi']}%\n📈 Активных вилок: {len(ACTIVE_ARBS)}")
                    elif text.startswith("/minroi "):
                        try:
                            val = float(text.split(" ")[1])
                            CONFIG["min_roi"] = val
                            send_tg_alert(f"✅ <b>Минимальный ROI изменен на {val}%</b>")
                        except Exception:
                            send_tg_alert("❌ Ошибка. Формат: /minroi 2.5")
                    elif text.startswith("/calc"):
                        handle_calc_command(text)
        except Exception as e:
            logging.debug(f"TG poll: {e}")
        time.sleep(2)


# ==========================================
# 4. API POLYMARKET
# ==========================================
def is_moneyline(m):
    mt = (m.get("sportsMarketType") or "").lower()
    if mt:
        return mt == "moneyline"
    q = m.get("question", "").lower()
    bad = ("game ", "map ", "spread", "handicap", "o/u", "total", "over", "under")
    return not any(w in q for w in bad)


def get_all_poly_events():
    global POLY_CACHE
    age = time.time() - POLY_CACHE["last_update"]

    if time.time() - POLY_CACHE["last_fail"] < 30:
        logging.warning(f"[Poly] недавняя ошибка API, беру кэш ({len(POLY_CACHE['events'])} матчей)")
        return POLY_CACHE["events"]

    if age < 60 and POLY_CACHE["events"]:
        logging.info(f"[Poly] кэш ({age:.0f}с назад): {len(POLY_CACHE['events'])} матчей")
        return POLY_CACHE["events"]

    t0 = time.time()
    logging.info("[Poly] обновляю список матчей...")
    all_events = []
    for offset in range(0, 1000, 100):
        url = "https://gamma-api.polymarket.com/events"
        params = {"tag_slug": "esports", "active": "true", "closed": "false", "limit": 100, "offset": offset}
        try:
            res = session.get(url, params=params, timeout=15).json()
            if not isinstance(res, list) or not res:
                break

            for ev in res:
                title = ev.get("title", "")
                names = split_title(title)
                if not names:
                    continue
                t1, t2 = names
                poly_url = f"https://polymarket.com/event/{ev.get('slug', '')}"

                for m in ev.get("markets", []):
                    if not m.get("active") or m.get("closed") or not is_moneyline(m):
                        continue
                    outcomes = json.loads(m.get("outcomes", '[]')) if isinstance(m.get("outcomes"), str) else m.get(
                        "outcomes", [])
                    tokens = json.loads(m.get("clobTokenIds", '[]')) if isinstance(m.get("clobTokenIds"),
                                                                                   str) else m.get("clobTokenIds", [])

                    if len(outcomes) != 2 or len(tokens) != 2:
                        continue
                    o0, o1 = str(outcomes[0]).strip(), str(outcomes[1]).strip()
                    if {o0.lower(), o1.lower()} & {"over", "under"}:
                        continue

                    if o0.lower() == "yes":
                        q = m.get("question", "").lower()
                        in1, in2 = t1.lower() in q, t2.lower() in q
                        if in1 == in2:
                            continue
                        if in2:
                            tokens = [tokens[1], tokens[0]]
                    else:
                        tokens = resolve_outcomes([o0, o1], tokens, t1, t2)
                        if not tokens:
                            logging.debug(f"Poly: не сопоставил исходы {o0}/{o1} с '{title}'")
                            continue

                    all_events.append({
                        "title": title, "team1": t1, "team2": t2, "tokens": tokens,
                        "url": poly_url, "fees": bool(m.get("feesEnabled", True))
                    })
                    break
            time.sleep(0.5)
        except Exception as e:
            logging.warning(f"Ошибка Poly API: {e}")
            POLY_CACHE["last_fail"] = time.time()

    unique_events = {f"{e['team1']}-{e['team2']}": e for e in all_events}

    if not all_events:
        POLY_CACHE["last_fail"] = time.time()
        return POLY_CACHE["events"]

    POLY_CACHE["events"] = list(unique_events.values())
    POLY_CACHE["last_update"] = time.time()

    logging.info(f"[Poly] загружено {len(POLY_CACHE['events'])} матчей за {time.time() - t0:.1f}с")
    return POLY_CACHE["events"]


def get_avg_execution_price(token_id, target_usd=50.0):
    try:
        res = session.get("https://clob.polymarket.com/book", params={"token_id": token_id}, timeout=5).json()
        asks = sorted(res.get("asks", []), key=lambda x: float(x["price"]))
        acc_usd, acc_shares = 0.0, 0.0
        for ask in asks:
            p, s = float(ask["price"]), float(ask["size"])
            if acc_usd + (p * s) >= target_usd:
                return target_usd / (acc_shares + (target_usd - acc_usd) / p)
            acc_usd += (p * s)
            acc_shares += s

        if acc_usd >= 10:
            logging.debug(f"Глубина стакана всего ${acc_usd:.0f}")
            return acc_usd / acc_shares
    except Exception:
        pass
    return 0.0


def poly_effective_odd(price, fees_enabled=True):
    if price <= 0:
        return 0.0
    rate = CONFIG["poly_fee_rate"] if fees_enabled else 0.0
    return (1.0 / price) * (1.0 - rate * (1.0 - price))


# ==========================================
# 5. МАТЕМАТИКА И АЛЕРТЫ
# ==========================================
def check_arb(poly_odd, bookie_odd, arb_key, label, poly_url, bookie_url, bookie_name="Winline", format_note="",
              gross_poly_price=None, is_live=False):
    if poly_odd <= 1.0 or bookie_odd <= 1.0:
        return None

    margin = (1.0 / poly_odd) + (1.0 / bookie_odd)
    roi = ((1.0 / margin) - 1.0) * 100
    mode_tag = "🔴 LIVE" if is_live else "🕒 Прематч"
    res = {"label": label, "margin": margin, "roi": roi, "status": "·", "mode": mode_tag}

    if margin < 1.0 and roi >= CONFIG["min_roi"]:
        now = time.time()
        last_alert = ACTIVE_ARBS.get(arb_key, {}).get("last_alert", 0)

        if now - last_alert < CONFIG["cooldown"]:
            res["status"] = "⏳ кулдаун"
            return res

        if arb_key not in ACTIVE_ARBS:
            ACTIVE_ARBS[arb_key] = {"start_time": now, "last_alert": 0, "miss_count": 0, "mode": mode_tag}
        ACTIVE_ARBS[arb_key]["last_alert"] = now
        ACTIVE_ARBS[arb_key]["miss_count"] = 0
        ACTIVE_ARBS[arb_key]["mode"] = mode_tag

        life_time = int(now - ACTIVE_ARBS[arb_key]["start_time"])
        status = "🆕 НОВАЯ ВИЛКА" if life_time < 5 else f"🔥 ЖИВА {life_time} сек."
        res["status"] = "🔥 ВИЛКА"

        stake_poly = CONFIG["bankroll"] / (poly_odd * margin)
        stake_bookie = CONFIG["bankroll"] / (bookie_odd * margin)
        net_profit = (stake_poly * poly_odd) - CONFIG["bankroll"]

        alert_text = (f"<b>{status}</b> | <b>{mode_tag}</b> [{bookie_name}]\n"
                      f"{label}\n"
                      f"Формат: {format_note}\n"
                      f"ROI: <b>+{roi:.2f}%</b> | Профит: +${net_profit:.2f}\n"
                      f"👉 <a href='{poly_url}'>Poly ({poly_odd:.2f} после комиссии)</a>: ${stake_poly:.2f}\n"
                      f"👉 <a href='{bookie_url}'>{bookie_name} ({bookie_odd:.2f})</a>: ${stake_bookie:.2f}")

        logging.info(
            "\n" + alert_text.replace('<b>', '').replace('</b>', '').replace('<a href=', '').replace("'>", ' ').replace(
                "</a>", '')
        )

        calc_params = None
        if gross_poly_price and gross_poly_price > 0:
            calc_params = f"p={gross_poly_price * 100:.1f}&w={bookie_odd}&b={CONFIG['bankroll']}&fee={CONFIG['poly_fee_rate'] * 100:.1f}&bk={bookie_name}"

        send_tg_alert(alert_text, calc_params)

    elif arb_key in ACTIVE_ARBS:
        ACTIVE_ARBS[arb_key]["miss_count"] += 1
        if ACTIVE_ARBS[arb_key]["miss_count"] >= 3:
            life_time = int(time.time() - ACTIVE_ARBS[arb_key]["start_time"])
            saved_mode = ACTIVE_ARBS[arb_key].get("mode", mode_tag)
            send_tg_alert(f"📉 <b>ВИЛКА ЗАКРЫЛАСЬ ({life_time} сек.)</b> | {saved_mode}\n{label}")
            del ACTIVE_ARBS[arb_key]

    return res


# ==========================================
# 6A. ПАРСИНГ WINLINE (ASYNC)
# ==========================================
async def scrape_winline_matches(page, page_url):
    matches_data = await page.evaluate(r"""() => {
        const results = [];
        const numRe = /^[0-9]+[.,][0-9]+$/;
        const info = Array.from(document.querySelectorAll('body *'))
            .filter(el => el.children.length === 0 && el.textContent && el.textContent.trim() !== '')
            .map(el => {
                const r = el.getBoundingClientRect();
                return { t: el.textContent.trim(), l: r.left, r: r.right, cy: r.top + r.height / 2, w: r.width, h: r.height };
            })
            .filter(o => o.w > 0 && o.h > 0);

        const labels = info.filter(o => 
            (o.t === 'Матч' || o.t === 'Исход' || o.t === 'Победитель') && 
            !/карт|раунд/i.test(o.t)
        );

        labels.forEach(lb => {
            const cells = info
                .filter(o => Math.abs(o.cy - lb.cy) < 12 && o.l >= lb.r - 1 && (numRe.test(o.t) || o.t === '-'))
                .sort((a, b) => a.l - b.l);

            if (cells.length < 2 || !numRe.test(cells[0].t) || !numRe.test(cells[1].t)) return;

            const names = info
                .filter(o => o.r <= lb.l + 1 && o.cy > lb.cy - 18 && o.cy < lb.cy + 28 && o.t.length >= 2 &&
                             !/^[0-9.,\-\s]+$/.test(o.t) && !/^\+\d+$/.test(o.t) && !/^\d+[KКkк]$/.test(o.t) && 
                             !/\d{1,2}:\d{2}/.test(o.t) && !/^\d{1,2}\.\d{2}(\.\d{2,4})?$/.test(o.t) &&
                             !/карта|раунд|фора|тотал|сегодня|завтра|победитель/i.test(o.t))
                .sort((a, b) => a.cy - b.cy);

            if (names.length < 2) return;
            const team2 = names.find(n => n.cy > names[0].cy + 8);
            if (!team2) return;

            results.push({ 
                team1: names[0].t, 
                team2: team2.t, 
                odd1: parseFloat(cells[0].t.replace(',', '.')), 
                odd2: parseFloat(cells[1].t.replace(',', '.')) 
            });
        });
        return results.filter((v, i, a) => a.findIndex(v2 => v2.team1 === v.team1 && v2.team2 === v.team2) === i);
    }""")
    for m in matches_data:
        m["url"] = page_url
        m["bookie"] = "Winline"
    return matches_data


async def scrape_winline_page_full(page, name, url, max_steps=40):
    found = {}
    await page.mouse.move(600, 500)
    await page.mouse.wheel(0, -100000)
    await page.wait_for_timeout(1000)
    stall = 0

    is_tab_live = ("live" in name.lower()) or ("live" in url.lower())

    for step in range(max_steps):
        before = len(found)
        matches = await scrape_winline_matches(page, url)
        for m in matches:
            m["is_live"] = m.get("is_live", False) or is_tab_live
            found[f"{m['team1']}|{m['team2']}"] = m
        stall = stall + 1 if len(found) == before else 0

        if step and step % 15 == 0:
            logging.info(f"[Winline] {name}: прокрутка {step}, найдено {len(found)}...")

        if stall >= 5:
            break
        await page.mouse.wheel(0, 700)
        await page.wait_for_timeout(350)

    logging.debug(f"[Winline] {name}: собрано {len(found)} матчей.")
    return list(found.values())


# ==========================================
# 6B. ПАРСИНГ 1WIN (ASYNC)
# ==========================================
async def scrape_1win_matches(page, page_url):
    matches_data = await page.evaluate(r"""() => {
        const results = [];
        const oddRe = /^[1-9][0-9]*[.,][0-9]{1,2}$/;

        const nodes = Array.from(document.querySelectorAll('body *'))
            .filter(el => el.children.length === 0 && el.textContent && el.textContent.trim() !== '')
            .map(el => {
                const r = el.getBoundingClientRect();
                return {
                    t: el.textContent.trim(),
                    l: r.left,
                    r: r.right,
                    top: r.top,
                    cy: r.top + r.height / 2,
                    w: r.width,
                    h: r.height
                };
            })
            .filter(o => o.w > 0 && o.h > 0 && o.top > 80);

        const odds = nodes.filter(o => oddRe.test(o.t)).map(o => ({
            ...o,
            val: parseFloat(o.t.replace(',', '.'))
        })).filter(o => o.val > 1.01 && o.val < 35.0);

        const usedOdds = new Set();

        for (let i = 0; i < odds.length; i++) {
            if (usedOdds.has(i)) continue;
            const o1 = odds[i];

            const pairIdx = odds.findIndex((o2, idx) =>
                idx !== i &&
                !usedOdds.has(idx) &&
                Math.abs(o2.cy - o1.cy) < 10 &&
                o2.l > o1.r &&
                (o2.l - o1.r) < 230
            );

            if (pairIdx === -1) continue;
            const o2 = odds[pairIdx];

            const middleOdd = odds.find((om, idx) =>
                idx !== i && idx !== pairIdx &&
                Math.abs(om.cy - o1.cy) < 10 &&
                om.l > o1.r && om.r < o2.l
            );
            if (middleOdd) continue;

            // Пропускаем рынки "Тотал / Фора" (Меньше/Больше, 31.5 и т.п.) — это не победитель матча
            const isTotalRow = nodes.some(n =>
                Math.abs(n.cy - o1.cy) < 10 && n.l > o1.l - 190 && n.r <= o2.r &&
                n !== o1 && n !== o2 && /меньше|больше|тотал|фора|^[+-]?\d+\.5\b|under|over/i.test(n.t));
            if (isTotalRow) continue;

            const candidates = nodes.filter(n =>
                n.r <= o1.l - 5 &&
                n.l > 180 &&
                Math.abs(n.cy - o1.cy) < 42 &&
                n.t.length >= 2 &&
                n.t.length <= 35 &&
                !/^[0-9.,:+\-\/\s]+$/.test(n.t) &&
                !/^\+\d+/.test(n.t) &&
                !/\b(bo\d|map|карта|фора|тотал|live|лайв|период|раунд|матч|исход|1x2|п1|п2|все)\b/i.test(n.t) &&
                !/^\d{1,2}:\d{2}$/.test(n.t) &&
                !/^(п[12x]|[12x]|х[12]|победитель|исход)$/i.test(n.t)
            ).sort((a, b) => a.cy - b.cy || a.l - b.l);

            if (candidates.length < 2) continue;

            const team1 = candidates[0];
            const team2 = candidates.find(c =>
                (Math.abs(c.cy - team1.cy) > 10 || Math.abs(c.l - team1.r) > 15) &&
                c.t.toLowerCase() !== team1.t.toLowerCase()
            );

            if (!team2) continue;

            usedOdds.add(i);
            usedOdds.add(pairIdx);

            results.push({
                team1: team1.t,
                team2: team2.t,
                odd1: o1.val,
                odd2: o2.val
            });
        }
        return results.filter((v, idx, arr) => arr.findIndex(v2 => v2.team1 === v.team1 && v2.team2 === v.team2) === idx);
    }""")
    for m in matches_data:
        m["url"] = page_url
        m["bookie"] = "1win"
    return matches_data


JS_1WIN_COLLAPSED = r"""(skip) => {
    // Свёрнутые группы турниров: заголовок = название + строка-подпись с игрой (CS2, Dota 2 ...) под ним.
    // Строки-ссылки с числом и стрелкой ">" (ведут на другую страницу) подписи не имеют — их не трогаем.
    const gameRe = /^(CS2|CS:?\s?GO|Dota\s?2|League of Legends|LoL|Valorant|Overwatch\s?2?|Rainbow Six.*|Mobile Legends.*|StarCraft.*|Call of Duty.*|Rocket League|King of Glory)$/i;
    const oddRe = /^[1-9][0-9]*[.,][0-9]{1,2}$/;
    const nodes = Array.from(document.querySelectorAll('body *'))
        .filter(el => el.children.length === 0 && el.textContent && el.textContent.trim() !== '')
        .map(el => { const r = el.getBoundingClientRect();
            return { t: el.textContent.trim(), l: r.left, r: r.right, cy: r.top + r.height / 2, w: r.width, h: r.height }; })
        .filter(o => o.w > 0 && o.h > 0);
    const heads = [];
    for (const g of nodes) {
        if (!gameRe.test(g.t) || g.l < 180) continue;
        const title = nodes.filter(n => n !== g && Math.abs(n.l - g.l) < 6 && g.cy - n.cy > 8 && g.cy - n.cy < 32 &&
                                        n.t.length >= 3 && !gameRe.test(n.t))
                           .sort((a, b) => b.cy - a.cy)[0];
        if (!title) continue;
        heads.push({ t: title.t, cy: (title.cy + g.cy) / 2, x: title.l + Math.min(120, title.w / 2) });
    }
    heads.sort((a, b) => a.cy - b.cy);
    for (let i = 0; i < heads.length; i++) {
        const h = heads[i];
        if (skip.includes(h.t)) continue;
        if (h.cy < 100 || h.cy > window.innerHeight - 20) continue;
        let collapsed;
        if (i + 1 < heads.length) {
            collapsed = heads[i + 1].cy - h.cy < 85;                 // шаг свёрнутых строк ~60px
        } else {
            if (window.innerHeight - h.cy < 140) continue;           // не видно что под ним — решим после прокрутки
            collapsed = !nodes.some(n => oddRe.test(n.t) && n.cy > h.cy + 20 && n.cy < h.cy + 130 && n.l > 300);
        }
        if (collapsed) return { t: h.t, x: h.x, y: h.cy };
    }
    return null;
}"""


async def expand_1win_groups(page, clicked, max_clicks=15):
    """Раскрывает свёрнутые турниры в текущем окне. clicked — заголовки, которые уже кликали."""
    n = 0
    while n < max_clicks:
        tgt = await page.evaluate(JS_1WIN_COLLAPSED, list(clicked))
        if not tgt:
            break
        clicked.add(tgt["t"])
        await page.mouse.move(tgt["x"], tgt["y"], steps=3)
        await page.mouse.click(tgt["x"], tgt["y"])
        await page.wait_for_timeout(350)
        n += 1
    if n:
        logging.debug(f"[1win] раскрыто групп: {n}")
    return n


async def scrape_1win_page_full(page, name, url, max_steps=35):
    found = {}
    await page.mouse.move(700, 500)
    await page.mouse.wheel(0, -100000)
    await page.wait_for_timeout(1000)

    is_tab_live = ("live" in name.lower()) or ("live" in url.lower())

    stall = 0
    clicked = set()
    for step in range(max_steps):
        before = len(found)
        await expand_1win_groups(page, clicked)
        matches = await scrape_1win_matches(page, url)
        for m in matches:
            m["is_live"] = m.get("is_live", False) or is_tab_live
            found[f"{m['team1']}|{m['team2']}"] = m

        stall = stall + 1 if len(found) == before else 0
        if stall >= 5:
            break

        await page.mouse.wheel(0, 650)
        await page.wait_for_timeout(400)

    logging.debug(f"[1win] {name}: собрано {len(found)} матчей.")
    return list(found.values())


# ==========================================
# 6C. ПАРСИНГ CSGOPOSITIVE (ASYNC: LIVE + UPCOMING СЛАЙДЕР)
# ==========================================
JS_EXTRACT_POSITIVE = r"""() => {
    const results = [];
    // На CSGOPositive коэффициенты могут иметь 2 или 3 знака после точки (1.211, 2.50 или x1.211)
    const oddRe = /^[x×]?\s*([1-9][0-9]*[.,][0-9]{2,3})$/i;

    const nodes = Array.from(document.querySelectorAll('body *'))
        .filter(el => el.children.length === 0 && el.textContent && el.textContent.trim() !== '')
        .map(el => {
            const r = el.getBoundingClientRect();
            return {
                t: el.textContent.trim(),
                l: r.left,
                r: r.right,
                top: r.top + window.scrollY,
                cy: r.top + window.scrollY + r.height / 2,
                cx: r.left + r.width / 2,
                w: r.width,
                h: r.height
            };
        })
        .filter(o => o.w > 0 && o.h > 0);

    // Находим координаты секций UPCOMING MATCHES и FINISHED MATCHES
    const _uh = (() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
        while ((n = w.nextNode())) {
            if (!/upcoming\s+matches/i.test((n.nodeValue || '').replace(/ /g, ' '))) continue;
            const rg = document.createRange(); rg.selectNodeContents(n);
            const r = rg.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return { el: n.parentElement, r };
        } return null; })();
    const upcomingHdr = _uh ? { top: _uh.r.top + window.scrollY } : null;
    const finishedHdr = nodes.find(n => /^FINISHED MATCHES$/i.test(n.t));

    const upcomingTop = upcomingHdr ? upcomingHdr.top : 999999;
    const finishedTop = finishedHdr ? finishedHdr.top : 999999;

    // Отбираем все видимые коэффициенты (выше блока FINISHED MATCHES)
    const odds = nodes
        .filter(n => n.top < finishedTop - 10 && oddRe.test(n.t))
        .map(n => {
            const m = n.t.match(oddRe);
            return { ...n, val: parseFloat(m[1].replace(',', '.')) };
        })
        .filter(o => o.val > 1.001 && o.val < 50.0);

    const used = new Set();

    // Группируем коэффициенты в пары (П1 и П2 находятся на одной горизонтальной линии cy)
    for (let i = 0; i < odds.length; i++) {
        if (used.has(i)) continue;
        const o1 = odds[i];

        // Ищем парный коэффициент справа в пределах одной карточки матча (ширина карточки ~250-550px)
        let bestIdx = -1;
        let bestDist = 9999;
        for (let j = 0; j < odds.length; j++) {
            if (i === j || used.has(j)) continue;
            const o2 = odds[j];
            const dy = Math.abs(o2.cy - o1.cy);
            const dx = o2.l - o1.r;
            if (dy < 18 && dx > 40 && dx < 520 && dx < bestDist) {
                bestDist = dx;
                bestIdx = j;
            }
        }

        if (bestIdx === -1) continue;
        const o2 = odds[bestIdx];

        // Ищем название Команды 1 (над или рядом с первым кэфом o1)
        const isTeamText = (txt) => {
            if (oddRe.test(txt)) return false;
            if (/^(LIVE|STREAM|LOSE|WIN|BO[1235]|VS|MAP|MATCH)$/i.test(txt)) return false;
            if (/^[0-9:.,+\-\/\s]+$/.test(txt)) return false;
            if (/^\d+\s+(OCT|NOV|DEC|JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP)/i.test(txt)) return false;
            return txt.length >= 2 && txt.length <= 32;
        };

        // В Live-карточках имя команды прямо над кэфом (на 15-45px выше, по тому же X).
        // В Upcoming-строках имя команды тоже прямо над кэфом (на 12-35px выше, по тому же X).
        const findTeamForOdd = (oddObj) => {
            const cands = nodes.filter(n =>
                isTeamText(n.t) &&
                n.cy < oddObj.cy - 4 &&
                n.cy > oddObj.cy - 65 &&
                Math.abs(n.cx - oddObj.cx) < 95
            ).sort((a, b) => Math.abs(oddObj.cy - a.cy) - Math.abs(oddObj.cy - b.cy));
            return cands.length ? cands[0].t : null;
        };

        const team1 = findTeamForOdd(o1);
        const team2 = findTeamForOdd(o2);

        if (!team1 || !team2 || team1.toLowerCase() === team2.toLowerCase()) continue;

        used.add(i);
        used.add(bestIdx);

        const isLive = o1.top < upcomingTop;
        results.push({
            team1: team1,
            team2: team2,
            odd1: o1.val,
            odd2: o2.val,
            is_live: isLive
        });
    }

    return results;
}"""


# Ищет в секции UPCOMING MATCHES: стрелку ">", стрелку "<" и точки-пагинатор.
# Возвращает КООРДИНАТЫ (клик делаем настоящей мышью, а не el.click()).
JS_POSITIVE_CONTROLS = r"""() => {
    const vis = (r) => r.width > 0 && r.height > 0;
    const _uh = (() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
        while ((n = w.nextNode())) {
            if (!/upcoming\s+matches/i.test((n.nodeValue || '').replace(/ /g, ' '))) continue;
            const rg = document.createRange(); rg.selectNodeContents(n);
            const r = rg.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return { el: n.parentElement, r };
        } return null; })();
    if (!_uh) return { hdr: null };
    const hdr = _uh.el, hr = _uh.r;
    const hy = hr.top + hr.height / 2;

    const isDisabled = (el) => {
        let e = el;
        for (let i = 0; i < 4 && e; i++, e = e.parentElement) {
            if (e.disabled || e.getAttribute('aria-disabled') === 'true') return true;
            if (/disabled/i.test(e.className && e.className.toString ? e.className.toString() : '')) return true;
            const op = parseFloat(getComputedStyle(e).opacity);
            if (op < 0.45) return true;
        }
        return false;
    };
    const info = (el, how) => {
        const r = el.getBoundingClientRect();
        return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width, h: r.height,
                 disabled: isDisabled(el), how,
                 html: (el.outerHTML || '').slice(0, 160) };
    };
    const clickable = (el) => {
        const cs = getComputedStyle(el);
        return el.tagName === 'BUTTON' || el.getAttribute('role') === 'button' ||
               cs.cursor === 'pointer' || el.tabIndex >= 0;
    };

    // Кандидаты: маленькие кликабельные элементы в строке заголовка справа от него
    const cands = Array.from(document.querySelectorAll('button, [role="button"], a, div, span, svg'))
        .filter(e => {
            const r = e.getBoundingClientRect();
            return vis(r) && r.width >= 14 && r.width <= 80 && r.height >= 14 && r.height <= 80 &&
                   Math.abs(r.top + r.height / 2 - hy) < 45 && r.left > hr.right + 20 && clickable(e);
        });
    // убираем вложенные (оставляем самый внешний кликабельный)
    const outer = cands.filter(e => !cands.some(o => o !== e && o.contains(e)));
    outer.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);

    let next = null, prev = null;
    // по SVG-стрелке вправо
    for (const p of document.querySelectorAll('path')) {
        const d = (p.getAttribute('d') || '').replace(/\s+/g, '');
        if (/M9,?5l7,?7-7,?7/.test(d) || /M8\.59,?16\.59/.test(d) || /M10,?6/.test(d)) {
            let e = p;
            for (let i = 0; i < 6 && e.parentElement; i++) {
                e = e.parentElement;
                if (clickable(e) && vis(e.getBoundingClientRect())) { next = info(e, 'svg-path'); break; }
            }
            if (next) break;
        }
    }
    if (outer.length >= 2) {
        prev = info(outer[outer.length - 2], 'row-prev');
        if (!next) next = info(outer[outer.length - 1], 'row-rightmost');
    } else if (outer.length === 1 && !next) {
        next = info(outer[0], 'row-single');
    }

    // Точки пагинации: ≥3 одинаковых маленьких элементов на одной горизонтали
    const small = Array.from(document.querySelectorAll('button, span, div, li')).filter(e => {
        const r = e.getBoundingClientRect();
        return vis(r) && r.width >= 4 && r.width <= 40 && r.height >= 4 && r.height <= 16 &&
               e.children.length === 0 && r.top > hr.top;
    });
    const rows = {};
    small.forEach(e => {
        const r = e.getBoundingClientRect();
        const k = Math.round((r.top + r.height / 2) / 6);
        (rows[k] = rows[k] || []).push(e);
    });
    let dots = [];
    Object.values(rows).forEach(g => { if (g.length >= 3 && g.length <= 30 && g.length > dots.length) dots = g; });
    dots.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);

    return {
        hdr: { x: hr.left, y: hy, scrollY: window.scrollY },
        next, prev,
        dots: dots.map(e => { const r = e.getBoundingClientRect();
            return { x: r.left + r.width / 2, y: r.top + r.height / 2, w: r.width }; }),
        vw: window.innerWidth, vh: window.innerHeight
    };
}"""


async def _positive_extract(page, url, found):
    """Забирает матчи и возвращает (всего_в_кадре, новых)."""
    batch = await page.evaluate(JS_EXTRACT_POSITIVE)
    new = 0
    for m in batch:
        m["url"] = url
        m["bookie"] = "CSGOPositive"
        key = f"{m['team1']}|{m['team2']}"
        if key not in found:
            found[key] = m
            new += 1
        else:
            # если матч уже был, но теперь пришёл с is_live — не теряем флаг
            found[key]["is_live"] = found[key].get("is_live", False) or m.get("is_live", False)
    return batch, new


async def _positive_wait_change(page, before_sig, timeout_ms=3500):
    """Ждём, пока слайд реально сменится (подпись изменится)."""
    waited = 0
    while waited < timeout_ms:
        await page.wait_for_timeout(350)
        waited += 350
        batch = await page.evaluate(JS_EXTRACT_POSITIVE)
        sig = tuple(sorted(f"{m['team1']}|{m['team2']}" for m in batch))
        if sig and sig != before_sig:
            await page.wait_for_timeout(300)  # дорисовка анимации
            return True
    return False


async def click_next_positive_slide(page, slide_idx, before_sig, ctrl=None):
    """
    Пробует по очереди несколько способов листания.
    Возвращает название сработавшего способа или None, если слайд не сменился.
    """
    ctrl = ctrl or await page.evaluate(JS_POSITIVE_CONTROLS)
    if not ctrl or not ctrl.get("hdr"):
        logging.warning("[CSGOPositive] не найден заголовок UPCOMING MATCHES")
        return None

    next_btn = ctrl.get("next")
    dots = ctrl.get("dots") or []
    row_y = ctrl["hdr"]["y"] + 140  # примерно середина карточек под заголовком
    vw = ctrl.get("vw", 1440)

    # Стрелка уже неактивна -> дальше листать некуда
    if next_btn and next_btn["disabled"] and not dots:
        logging.info("[CSGOPositive] стрелка '>' неактивна — конец слайдера")
        return None

    strategies = []
    if next_btn and not next_btn["disabled"]:
        strategies.append(("клик по '>' (" + next_btn["how"] + ")", "click", next_btn))
    if dots and slide_idx + 1 < len(dots):
        strategies.append((f"клик по точке {slide_idx + 2}/{len(dots)}", "click", dots[slide_idx + 1]))
    strategies.append(("свайп мышью", "drag", None))
    strategies.append(("клавиша →", "key", None))

    for label, kind, target in strategies:
        try:
            if kind == "click":
                await page.mouse.move(target["x"], target["y"], steps=4)
                await page.mouse.down()
                await page.wait_for_timeout(60)
                await page.mouse.up()
            elif kind == "drag":
                x0, x1 = vw * 0.78, vw * 0.22
                await page.mouse.move(x0, row_y)
                await page.mouse.down()
                await page.mouse.move(x1, row_y, steps=18)
                await page.mouse.up()
            else:
                await page.keyboard.press("ArrowRight")
        except Exception as e:
            logging.debug(f"[CSGOPositive] {label}: ошибка {e}")
            continue

        if await _positive_wait_change(page, before_sig):
            return label
        logging.debug(f"[CSGOPositive] {label}: слайд не сменился")
    return None


async def reset_positive_slider(page, ctrl):
    """Возвращает слайдер на 1-ю страницу (после прошлого цикла он стоит на последней)."""
    async def sig_now():
        b = await page.evaluate(JS_EXTRACT_POSITIVE)
        return tuple(sorted(f"{m['team1']}|{m['team2']}" for m in b))

    async def click(t):
        await page.mouse.move(t["x"], t["y"], steps=4)
        await page.mouse.down()
        await page.wait_for_timeout(60)
        await page.mouse.up()

    if not ctrl or not ctrl.get("hdr"):
        return
    start = await sig_now()
    steps = 0
    # 1) быстрый путь: клик по первой точке пагинации
    dots = ctrl.get("dots") or []
    if len(dots) >= 2:
        await click(dots[0])
        await _positive_wait_change(page, start, timeout_ms=1500)
        steps += 1
    # 2) докрутка кнопкой '<' пока слайд меняется
    for _ in range(20):
        c = await page.evaluate(JS_POSITIVE_CONTROLS)
        prev = c.get("prev") if c else None
        if not prev or prev["disabled"]:
            break
        before = await sig_now()
        await click(prev)
        steps += 1
        if not await _positive_wait_change(page, before, timeout_ms=1500):
            break
    logging.info(f"[CSGOPositive] слайдер возвращён на 1-ю страницу (действий: {steps})")


async def scrape_positive_page_full(page, name, url, max_slides=15):
    found = {}

    # Фоновая вкладка в headed-Chromium замораживает анимации и таймеры слайдера
    try:
        await page.bring_to_front()
    except Exception:
        pass

    # 1. Вверх, затем плавно вниз — чтобы прогрузились LIVE и UPCOMING (lazy-load)
    await page.mouse.move(700, 500)
    await page.mouse.wheel(0, -100000)
    await page.wait_for_timeout(700)
    for _ in range(4):
        await page.mouse.wheel(0, 400)
        await page.wait_for_timeout(450)
        await _positive_extract(page, url, found)
    live_count = len(found)

    # 2. Центрируем секцию UPCOMING MATCHES
    await page.evaluate(r"""() => {
        const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
        while ((n = w.nextNode())) {
            if (/upcoming\s+matches/i.test((n.nodeValue || '').replace(/\u00a0/g, ' ')) && n.parentElement) {
                n.parentElement.scrollIntoView({ behavior: 'instant', block: 'start' });
                window.scrollBy(0, -120);
                break;
            }
        }
    }""")
    await page.wait_for_timeout(900)

    ctrl = await page.evaluate(JS_POSITIVE_CONTROLS)
    if ctrl and ctrl.get("hdr"):
        n = ctrl.get("next")
        logging.info(
            f"[CSGOPositive] слайдер: стрелка={'да (' + n['how'] + ', disabled=' + str(n['disabled']) + ')' if n else 'НЕТ'}, "
            f"точек пагинации={len(ctrl.get('dots') or [])}")
        if n:
            logging.debug(f"[CSGOPositive] HTML стрелки: {n['html']}")

    # 2.5. Возвращаемся на первую страницу слайдера
    await reset_positive_slider(page, ctrl)
    ctrl = None

    # 3. Листаем слайдер
    seen_sigs = set()
    for slide in range(max_slides):
        batch, new_cnt = await _positive_extract(page, url, found)
        sig = tuple(sorted(f"{m['team1']}|{m['team2']}" for m in batch))
        logging.info(f"[CSGOPositive] слайд {slide + 1}: в кадре {len(batch)}, новых {new_cnt}, всего {len(found)}")

        if sig in seen_sigs:
            logging.info("[CSGOPositive] слайд повторился — это конец (зациклилось).")
            break
        seen_sigs.add(sig)

        how = await click_next_positive_slide(page, slide, sig, ctrl if slide == 0 else None)
        if not how:
            logging.info(f"[CSGOPositive] дальше листать нельзя (слайд {slide + 1} последний).")
            break
        logging.debug(f"[CSGOPositive] листание сработало: {how}")
        ctrl = None  # перечитаем координаты на следующем шаге

    logging.debug(f"[CSGOPositive] {name}: live/верх {live_count}, итого {len(found)}")
    return list(found.values())


# ==========================================
# 7. ОБЩИЕ ФУНКЦИИ ДЛЯ ОТКРЫТИЯ И СБОРА
# ==========================================
async def open_bookie_pages(context, urls_dict, source_name):
    pages = {}
    if source_name == "Winline":
        fn = scrape_winline_page_full
    elif source_name == "1win":
        fn = scrape_1win_page_full
    else:
        fn = scrape_positive_page_full

    for name, url in urls_dict.items():
        if not url or "https://" not in url:
            continue
        pg = await context.new_page()
        try:
            await pg.goto(url, timeout=60000, wait_until="domcontentloaded")
            await pg.wait_for_timeout(8000)
            await pg.keyboard.press("Escape")
            await pg.wait_for_timeout(500)
        except Exception as e:
            logging.warning(f"[{name}] не открылась: {e}")

        pages[name] = {"page": pg, "url": url, "source": source_name, "fn": fn}
    return pages


async def scrape_single_tab(name, data):
    t0 = time.time()
    try:
        res = await data["fn"](data["page"], name, data["url"])
        logging.info(f"[{data['source']}] {name}: {len(res)} матчей за {time.time() - t0:.1f}с")
        return res
    except Exception as e:
        logging.warning(f"[{data['source']}] {name}: ошибка ({e}), перезагружаю")
        try:
            await data["page"].reload(wait_until="domcontentloaded")
            await data["page"].wait_for_timeout(8000)
            await data["page"].keyboard.press("Escape")
        except Exception:
            pass
        return []


async def scrape_all_bookies(pages):
    tasks = [scrape_single_tab(name, data) for name, data in pages.items()]
    results = await asyncio.gather(*tasks)
    return [match for tab_matches in results for match in tab_matches]


# ==========================================
# 8. ОСНОВНОЙ ЦИКЛ (ASYNC)
# ==========================================
async def run_auto_scraper():
    threading.Thread(target=tg_bot_thread, daemon=True).start()
    logging.info("--- ЗАПУСК СКРИПТА (WINLINE + 1WIN + CSGOPOSITIVE) ---")

    try:
        async with async_playwright() as p:
            launch_kwargs = {
                "headless": CONFIG["headless"],
                "args": [
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding"
                ]
            }
            if CONFIG["pw_proxy"]:
                launch_kwargs["proxy"] = CONFIG["pw_proxy"]

            browser = await p.chromium.launch(**launch_kwargs)
            context = await browser.new_context(
                user_agent=HEADERS["User-Agent"],
                viewport={"width": 1440, "height": 900}
            )

            logging.info("Открываю все вкладки (Winline, 1win, CSGOPositive)...")
            win_pages = await open_bookie_pages(context, WINLINE_URLS, "Winline")
            onewin_pages = await open_bookie_pages(context, ONEWIN_URLS, "1win")
            pos_pages = await open_bookie_pages(context, POSITIVE_URLS, "CSGOPositive")

            all_pages = {**win_pages, **onewin_pages, **pos_pages}

            cycle = 0

            while True:
                if BOT_STATE["paused"]:
                    await asyncio.sleep(5)
                    continue

                cycle += 1
                t_cycle = time.time()
                logging.info(f"═══ ЦИКЛ #{cycle} ═══════════════════════")

                poly_events = get_all_poly_events()
                raw_bookie_matches = await scrape_all_bookies(all_pages)

                if not raw_bookie_matches:
                    logging.warning("Ни одна БК не вернула матчи в этом цикле. Пауза 15с...")
                    await asyncio.sleep(15)
                    continue

                # Дедупликация внутри каждой БК с приоритетом Live
                dedup_bookies = {}
                for bm in raw_bookie_matches:
                    t1 = re.sub(r'\(.*?\)|\.\.\.', '', bm['team1']).strip()
                    t2 = re.sub(r'\(.*?\)|\.\.\.', '', bm['team2']).strip()
                    if bm['odd1'] <= 1.0 or bm['odd2'] <= 1.0:
                        continue

                    margin = (1.0 / bm['odd1']) + (1.0 / bm['odd2'])
                    if margin > 1.20 or margin < 0.98:
                        logging.debug(f"[{bm['bookie']}] Пропуск (маржа {margin:.2f}): {t1} vs {t2}")
                        continue

                    key = f"{bm['bookie']}|{t1}|{t2}"
                    prev_live = dedup_bookies.get(key, {}).get('is_live', False)
                    is_live = bm.get('is_live', False) or prev_live

                    dedup_bookies[key] = {
                        'bookie': bm['bookie'], 'team1': t1, 'team2': t2,
                        'odd1': bm['odd1'], 'odd2': bm['odd2'], 'url': bm['url'],
                        'is_live': is_live
                    }

                valid_matches = list(dedup_bookies.values())

                pairs = unmatched = arbs = 0
                processed_keys = set()
                orderbook_cache = {}

                for bm in valid_matches:
                    bk = bm['bookie']
                    best_poly = None
                    highest_score = 0
                    is_swapped = False

                    b1, b2 = norm(bm['team1']), norm(bm['team2'])
                    m1_bk, m2_bk = get_markers(bm['team1']), get_markers(bm['team2'])

                    for poly in poly_events:
                        p1, p2 = norm(poly['team1']), norm(poly['team2'])
                        m1_poly, m2_poly = get_markers(poly['team1']), get_markers(poly['team2'])
                        direct_ok = m1_bk == m1_poly and m2_bk == m2_poly
                        swapped_ok = m1_bk == m2_poly and m2_bk == m1_poly

                        score_direct = min(fuzz.token_set_ratio(b1, p1),
                                           fuzz.token_set_ratio(b2, p2)) if direct_ok else 0
                        score_swapped = min(fuzz.token_set_ratio(b1, p2),
                                            fuzz.token_set_ratio(b2, p1)) if swapped_ok else 0
                        max_score = max(score_direct, score_swapped)

                        if max_score > highest_score:
                            highest_score = max_score
                            best_poly = poly
                            is_swapped = (score_swapped > score_direct)

                    title = f"[{bk}] {bm['team1']} vs {bm['team2']}"

                    if not (best_poly and highest_score >= CONFIG["match_threshold"]):
                        unmatched += 1
                        continue

                    pairs += 1
                    target_stake = CONFIG["bankroll"] / 2
                    p_tok1 = best_poly['tokens'][1] if is_swapped else best_poly['tokens'][0]
                    p_tok2 = best_poly['tokens'][0] if is_swapped else best_poly['tokens'][1]

                    if p_tok1 not in orderbook_cache:
                        orderbook_cache[p_tok1] = get_avg_execution_price(p_tok1, target_stake)
                    if p_tok2 not in orderbook_cache:
                        orderbook_cache[p_tok2] = get_avg_execution_price(p_tok2, target_stake)

                    price1, price2 = orderbook_cache[p_tok1], orderbook_cache[p_tok2]

                    if price1 <= 0 or price2 <= 0:
                        logging.info(f"[Пара] {title} | пустой стакан Poly, пропуск")
                        continue

                    p_odd1 = poly_effective_odd(price1, best_poly["fees"])
                    p_odd2 = poly_effective_odd(price2, best_poly["fees"])

                    bo_format = "Неизвестно"
                    mo = re.search(r'BO\d', best_poly.get("title", ""))
                    if mo:
                        bo_format = mo.group(0)

                    key1 = f"{bk}_{norm(bm['team1'])}_{norm(bm['team2'])}_dir1"
                    key2 = f"{bk}_{norm(bm['team1'])}_{norm(bm['team2'])}_dir2"
                    processed_keys.update((key1, key2))

                    results = [r for r in (
                        check_arb(p_odd1, bm['odd2'], key1,
                                  f"Poly [{bm['team1']}] + {bk} [{bm['team2']}]",
                                  best_poly['url'], bm['url'], bk, bo_format,
                                  gross_poly_price=price1, is_live=bm['is_live']),
                        check_arb(p_odd2, bm['odd1'], key2,
                                  f"Poly [{bm['team2']}] + {bk} [{bm['team1']}]",
                                  best_poly['url'], bm['url'], bk, bo_format,
                                  gross_poly_price=price2, is_live=bm['is_live']),
                    ) if r]

                    if results:
                        best = min(results, key=lambda r: r["margin"])
                        if best["status"] == "🔥 ВИЛКА":
                            arbs += 1
                        logging.info(
                            f"[Пара] {best['mode']} {title} ({highest_score:.0f}%) | "
                            f"Poly {p_odd1:.2f}/{p_odd2:.2f} | {bk} {bm['odd1']:.2f}/{bm['odd2']:.2f} | "
                            f"маржа {best['margin']:.3f} ({best['roi']:+.1f}%) {best['status']}"
                        )

                for k in list(ACTIVE_ARBS.keys()):
                    if k not in processed_keys:
                        del ACTIVE_ARBS[k]

                logging.info(
                    f"[Итог] цикл #{cycle}: матчей БК {len(valid_matches)} | пар {pairs} | вилок {arbs} | {time.time() - t_cycle:.0f}с"
                )

                await asyncio.sleep(15)

    except KeyboardInterrupt:
        logging.info("Остановлено пользователем.")
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}")
        send_tg_alert(f"⚠️ <b>КРИТИЧЕСКАЯ ОШИБКА:</b>\n{e}")


if __name__ == "__main__":
    asyncio.run(run_auto_scraper())