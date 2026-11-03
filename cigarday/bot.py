#!/usr/bin/env python3
"""Тестовый Telegram-бот CigarDay: персональные подборки сигар и оценка
своих покупок. Прототип «посмотреть и пощупать», не боевой запуск.

Это НЕ @cigarindex_bot: свой токен CIGARDAY_TESTBOT_TOKEN в
/opt/cigarindex/.env (общий .env сервера-сборщика). Работает только на сервере-сборщике — Telegram
отсюда доступен лишь по IPv6 (curl -6), как в ops/notify_shops.py CigarIndex.

Без вебхука: long-poll getUpdates (timeout=25), поэтому нажатие звезды
отзывается сразу, а снаружи на сервер ничего не открывается. Запуск —
systemd-сервис deploy/cigarday-bot.service.

Состояние (история покупок, оценки, согласие) — по файлу на chat_id в
/var/lib/cigarday-bot/, вне репозитория: репозиторий публичный, и
историям покупок в нём не место.
Файлы 0600. /stop удаляет файл пользователя целиком.

На этом шаге НЕТ рассылки, заказов и СБИС. 15-ФЗ: никаких скидок, акций,
призывов купить и кнопок «заказать» — цена и наличие только как информация.

  python3 cigarday/bot.py            # работать (нужен токен)
  python3 cigarday/bot.py --once     # один проход getUpdates и выйти
"""
import datetime
import html
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import traceback

import paths  # noqa: E402
import reco as R  # noqa: E402
import reco_match as M  # noqa: E402
import reco_text as T  # noqa: E402

TOKEN = os.environ.get("CIGARDAY_TESTBOT_TOKEN", "").strip()
STATE = pathlib.Path(os.environ.get("CIGARDAY_BOT_STATE", "/var/lib/cigarday-bot"))
USERS = STATE / "users"
PRICES = paths.PRICES
PROFILES = paths.PROFILES
RATE_MAX = 15          # сколько покупок показывать для оценки


def log(*a):
    print(datetime.datetime.now().isoformat(timespec="seconds"), *a, flush=True)


# ── Telegram ───────────────────────────────────────────────────────────

def tg(method, **params):
    """Вызов Bot API через curl -6. Ни токен, ни данные пользователя (его
    история покупок) не попадают в аргументы процесса, которые видно в ps:
    URL с токеном — в stdin-конфиге curl, тело — JSON во временном файле 0600.
    На 429 (слишком часто) ждём, сколько просит Telegram, и повторяем раз:
    подборка — это ~10 сообщений подряд, и карточки не должны молча пропадать."""
    cfg = f'url = "https://api.telegram.org/bot{TOKEN}/{method}"\n'
    STATE.mkdir(parents=True, exist_ok=True)
    fd, body = tempfile.mkstemp(dir=STATE, prefix=".req-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(params, f, ensure_ascii=False)
        args = ["curl", "-6", "-s", "--max-time", "40", "-K", "-",
                "-H", "Content-Type: application/json", "--data-binary", f"@{body}"]
        for attempt in (1, 2):
            r = subprocess.run(args, input=cfg, capture_output=True, text=True)
            try:
                out = json.loads(r.stdout)
            except ValueError:
                log(f"tg {method}: нет ответа (curl {r.returncode})")
                return None
            wait = (out.get("parameters") or {}).get("retry_after")
            if out.get("error_code") == 429 and wait and attempt == 1:
                time.sleep(min(int(wait), 30) + 1)
                continue
            break
    finally:
        os.unlink(body)
    if not out.get("ok"):
        log(f"tg {method}: {out.get('description')}")
    return out


def send(chat, text, kb=None, preview=False):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML",
         "link_preview_options": {"is_disabled": not preview}}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    return tg("sendMessage", **p)


def send_photo(chat, url, caption):
    """Фото с подписью; не вышло (ссылка, формат) — тем же текстом без фото,
    подборка не должна теряться из-за картинки."""
    if url:
        r = tg("sendPhoto", chat_id=chat, photo=url, caption=caption[:1024], parse_mode="HTML")
        if r and r.get("ok"):
            return r
    return send(chat, caption)


# ── хранилище ──────────────────────────────────────────────────────────

def _upath(chat):
    return USERS / f"{int(chat)}.json"


def load_user(chat):
    try:
        return json.loads(_upath(chat).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def save_user(chat, u):
    USERS.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    tmp = _upath(chat).with_suffix(".tmp")
    tmp.write_text(json.dumps(u, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, _upath(chat))


def delete_user(chat):
    try:
        _upath(chat).unlink()
        return True
    except FileNotFoundError:
        return False


def load_offset():
    try:
        return json.loads((STATE / "offset.json").read_text())["offset"]
    except (FileNotFoundError, ValueError, KeyError):
        return 0


def save_offset(n):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "offset.json").write_text(json.dumps({"offset": n}))


# ── данные ─────────────────────────────────────────────────────────────

_cat = {"mtime": None, "catalog": None, "aliases": None, "idf": None}


def catalog():
    """Каталог перечитываем, когда ночной сбор или дневная перепроверка
    наличия обновили prices.json — иначе бот показывал бы вчерашнее."""
    mt = (PRICES.stat().st_mtime, PROFILES.stat().st_mtime)
    if _cat["mtime"] != mt:
        prices = json.loads(PRICES.read_text(encoding="utf-8"))
        prof = json.loads(PROFILES.read_text(encoding="utf-8"))
        _cat.update(mtime=mt, catalog=R.build_catalog(prices, prof), aliases=M.load_aliases())
        _cat["idf"] = R.note_idf(_cat["catalog"])
        log(f"каталог перечитан: в наличии у CigarDay {sum(1 for x in _cat['catalog'].values() if x['in_stock'])}")
    return _cat["catalog"], _cat["aliases"], _cat["idf"]


# ── тексты ─────────────────────────────────────────────────────────────

HELLO = ("Здравствуйте! Это тестовый бот CigarDay: он подбирает сигары по вашему вкусу — "
         "из того, что вы уже курили и как это оценили.\n\n"
         "<i>Для подбора бот хранит историю ваших покупок и ваши оценки. Удалить всё можно "
         "командой /stop. Нажимая «Согласен», вы соглашаетесь на обработку этих данных (152-ФЗ).</i>")

MENU = ("Что можно сделать:\n\n"
        "/demo1 — пример: любитель кубинской классики\n"
        "/demo2 — пример: Новый Свет, покрепче\n"
        "/load — загрузить свою историю покупок текстом\n"
        "/rate — оценить свои покупки\n"
        "/pick — подборка\n"
        "/stop — удалить мои данные")

LOAD_HELP = ("Пришлите историю покупок одним сообщением — по строке на покупку:\n\n"
             "<code>название; дата; количество; цена за штуку</code>\n\n"
             "Например:\n<code>Partagas Serie D No. 4; 14.10.2026; 5; 2750\n"
             "Montecristo No. 2; 20.09.2026; 3; 3250</code>\n\n"
             "Можно и сразу: <code>/load</code> и строки ниже в том же сообщении.")

KB_AFTER_PICK = [[{"text": "★ Оценить покупки", "callback_data": "rate"},
                  {"text": "↻ Обновить подборку", "callback_data": "pick"}]]


def stars_kb(cid, cur):
    row = [{"text": ("★" if cur and n <= cur else "☆") + (f" {n}" if n == cur else ""),
            "callback_data": f"r:{n}:{cid}"[:64]} for n in range(1, 6)]
    return [row]


# ── сценарии ───────────────────────────────────────────────────────────

def need_consent(chat, u):
    if u.get("consent"):
        return False
    send(chat, HELLO, [[{"text": "Согласен", "callback_data": "consent"}]])
    return True


def do_load(chat, u, text, source):
    cat, aliases, _ = catalog()
    rows, unknown = M.parse_history(text, aliases)
    if not rows:
        send(chat, "Не узнал ни одной сигары. " + ("Не нашлись: " + html.escape(
            "; ".join(x["name"] for x in unknown[:10])) + "\n\n" if unknown else "") + LOAD_HELP)
        return
    u.update(history=rows, ratings={}, source=source, awaiting_load=False,
             loaded_at=datetime.date.today().isoformat())
    save_user(chat, u)
    uniq = list(dict.fromkeys(r["cid"] for r in rows))
    msg = [f"Загружено покупок: {len(rows)}, разных сигар: {len(uniq)}."]
    if unknown:
        msg.append("Не узнал (в подборке не учтены): " + html.escape("; ".join(x["name"] for x in unknown[:10])))
    msg.append("\nТеперь можно оценить их — /rate, или сразу посмотреть подборку — /pick.")
    send(chat, "\n".join(msg), KB_AFTER_PICK)


def do_rate(chat, u):
    hist = u.get("history") or []
    if not hist:
        send(chat, "Покупок пока нет. Загрузите историю — /load, или возьмите пример — /demo1, /demo2.")
        return
    cat, _, _ = catalog()
    st = R.purchase_stats(hist)
    order = sorted(st, key=lambda c: st[c]["last"] or datetime.date.min, reverse=True)[:RATE_MAX]
    send(chat, "<b>Ваши покупки</b>\nОцените каждую — одно нажатие. Оценку можно поменять в любой момент.\n"
               "<i>5–4 — люблю, 3 — так себе, 2–1 — не моё.</i>")
    for cid in order:
        it, s = cat[cid], st[cid]
        d = s["last"].strftime("%d.%m.%Y") if s["last"] else ""
        if s["times"] > 1:
            line = f"брали {s['times']} раз{'а' if 2 <= s['times'] <= 4 else ''}" + (f", последний — {d}" if d else "")
        else:
            line = f"брали {d}" if d else "брали один раз"
        send(chat, f"<b>{html.escape(it['name'])}</b>\n{line}", stars_kb(cid, u.get("ratings", {}).get(cid)))
    send(chat, "Когда закончите — обновите подборку: оценки учитываются сразу.",
         [[{"text": "↻ Обновить подборку", "callback_data": "pick"}]])


def do_pick(chat, u):
    hist = u.get("history") or []
    if not hist:
        send(chat, "Чтобы подобрать, нужна история покупок: /load — своя, /demo1 или /demo2 — пример.")
        return
    cat, _, idf = catalog()
    res = R.recommend(hist, u.get("ratings") or {}, cat)
    rated = sum(1 for v in (u.get("ratings") or {}).values() if v)
    head = "<b>Ваша подборка</b>\n<i>По вашим покупкам" + (f" и {rated} оценкам" if rated else "") + \
           f". Цены и наличие CigarDay на {datetime.date.today().strftime('%d.%m.%Y')}.</i>"
    send(chat, head)

    send(chat, "<b>Ваши любимые — есть в наличии</b>")
    if res["favorites"]:
        for f in res["favorites"]:
            send_photo(chat, f["item"]["image"], T.card(f["item"], f["reason"], idf))
    else:
        send(chat, "<i>Сейчас ни одной из ваших любимых нет в наличии.</i>")
    if res["favorites_absent"]:
        send(chat, "<i>Сейчас нет в наличии: " +
             html.escape(", ".join(x["name"] for x in res["favorites_absent"])) + "</i>")

    sub = ""
    if res.get("price"):
        sub = (f"\n<i>Похожие по вкусу, крепости и формату — около вашей обычной цены "
               f"{T.rub(res['price'])} за штуку.</i>")
    send(chat, "<b>Вам может понравиться</b>" + sub)
    if res["suggestions"]:
        for s in res["suggestions"]:
            send_photo(chat, s["item"]["image"], T.card(s["item"], s["reason"], idf))
    else:
        send(chat, "<i>Пока не из чего подобрать: оцените покупки повыше или загрузите больше истории.</i>")
    send(chat, "Оценки меняют подборку — попробуйте.", KB_AFTER_PICK)


def on_message(m):
    chat = m["chat"]["id"]
    if m["chat"].get("type") != "private":
        return
    text = (m.get("text") or "").strip()
    cmd, _, rest = text.partition("\n") if text.startswith("/") else ("", "", text)
    cmd, _, arg = cmd.partition(" ")
    cmd = cmd.split("@")[0].lower()
    rest = (arg + "\n" + rest).strip() if cmd else rest
    u = load_user(chat)

    if cmd == "/stop":
        had = delete_user(chat)
        send(chat, "Готово: история покупок, оценки и согласие удалены." if had else "У меня нет ваших данных.")
        return
    if cmd == "/start":
        if u.get("consent"):
            send(chat, MENU)
        else:
            send(chat, HELLO, [[{"text": "Согласен", "callback_data": "consent"}]])
        return
    if need_consent(chat, u):
        return
    if cmd in ("/demo1", "/demo2"):
        title, text_ = T.DEMOS[cmd[1:]]
        send(chat, f"Загружаю пример: <b>{html.escape(title)}</b>.")
        do_load(chat, u, text_, cmd[1:])
    elif cmd == "/load":
        if rest:
            do_load(chat, u, rest, "load")
        else:
            u["awaiting_load"] = True
            save_user(chat, u)
            send(chat, LOAD_HELP)
    elif cmd == "/rate":
        do_rate(chat, u)
    elif cmd == "/pick":
        do_pick(chat, u)
    elif cmd in ("/help", "/menu"):
        send(chat, MENU)
    elif not cmd and u.get("awaiting_load"):
        do_load(chat, u, text, "load")
    else:
        send(chat, MENU)


def on_callback(q):
    chat = q["message"]["chat"]["id"]
    data = q.get("data") or ""
    u = load_user(chat)
    if data == "consent":
        u["consent"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_user(chat, u)
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Спасибо!")
        tg("editMessageReplyMarkup", chat_id=chat, message_id=q["message"]["message_id"],
           reply_markup={"inline_keyboard": []})
        send(chat, MENU)
        return
    if not u.get("consent"):
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Сначала /start")
        return
    if data.startswith("r:"):
        _, n, cid = data.split(":", 2)
        n = int(n)
        if cid not in {h["cid"] for h in u.get("history") or []}:
            tg("answerCallbackQuery", callback_query_id=q["id"], text="Этой покупки уже нет в истории")
            return
        u.setdefault("ratings", {})[cid] = n
        save_user(chat, u)
        tg("answerCallbackQuery", callback_query_id=q["id"], text=f"Оценка {n} сохранена")
        tg("editMessageReplyMarkup", chat_id=chat, message_id=q["message"]["message_id"],
           reply_markup={"inline_keyboard": stars_kb(cid, n)})
    elif data == "pick":
        tg("answerCallbackQuery", callback_query_id=q["id"], text="Подбираю…")
        do_pick(chat, u)
    elif data == "rate":
        tg("answerCallbackQuery", callback_query_id=q["id"])
        do_rate(chat, u)
    else:
        tg("answerCallbackQuery", callback_query_id=q["id"])


def poll(timeout=25):
    off = load_offset()
    r = tg("getUpdates", offset=off + 1, timeout=timeout,
           allowed_updates=["message", "callback_query"])
    if not r or not r.get("ok"):
        time.sleep(5)
        return
    for upd in r.get("result", []):
        # offset сохраняем ДО разбора: одно сломанное сообщение не должно
        # повторяться бесконечно и стопорить бота всем остальным.
        save_offset(max(off, upd["update_id"]))
        off = max(off, upd["update_id"])
        try:
            if "message" in upd:
                on_message(upd["message"])
            elif "callback_query" in upd:
                on_callback(upd["callback_query"])
        except Exception as e:                       # noqa: BLE001
            log(f"сообщение {upd.get('update_id')} пропущено — {type(e).__name__}: {e}")
            traceback.print_exc()


def main():
    if not TOKEN:
        log("нет CIGARDAY_TESTBOT_TOKEN в /opt/cigarindex/.env — жду минуту и выхожу")
        time.sleep(60)
        return 0
    tg("setMyCommands", commands=[
        {"command": "pick", "description": "Подборка"},
        {"command": "rate", "description": "Оценить покупки"},
        {"command": "demo1", "description": "Пример: кубинская классика"},
        {"command": "demo2", "description": "Пример: Новый Свет"},
        {"command": "load", "description": "Загрузить свою историю"},
        {"command": "stop", "description": "Удалить мои данные"},
    ])
    once = "--once" in sys.argv
    log("бот запущен")
    while True:
        poll(0 if once else 25)
        if once:
            return 0


if __name__ == "__main__":
    sys.exit(main())
