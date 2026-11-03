#!/usr/bin/env python3
"""Тестовый Telegram-бот CigarDay: персональные подборки сигар и оценка
своих покупок. Прототип «посмотреть и пощупать», не боевой запуск.

Токен — CIGARDAY_TESTBOT_TOKEN в /opt/cigarindex/.env (общий .env
сервера-сборщика). Работает только на сервере-сборщике: Telegram отсюда
доступен лишь по IPv6.

Интерфейс — чтобы чат не засыпало сообщениями:
  • меню внизу экрана: «Подборка», «Оценить покупки», «Загрузить историю», «Помощь»;
  • подборка — ОДНО сообщение-карусель: фото + карточка, вкладки
    «Любимые» / «Вам понравится», стрелки ◀ 1/3 ▶ (листание правит то же
    сообщение);
  • на карточке — «📌 Забронировать» (заявка уходит менеджеру CigarDay,
    он связывается с клиентом; покупка и оплата — в магазине) и ссылка на
    страницу сигары на cigarday.ru;
  • оценка — тоже одно сообщение: фото покупки и пять звёзд; после
    нажатия бот сам переходит к следующей, в конце — «Показать подборку».

Сеть. Каждая третья новая TCP-сессия до api.telegram.org по IPv6 с этого
сервера зависает (замерено 03.11.2026: 0,1 с или висит до таймаута). Поэтому
одно постоянное соединение (keep-alive), короткий таймаут на установку и
повтор. Токен и данные пользователя не покидают процесс — никаких curl с
аргументами, видимыми в ps.

Состояние — по файлу на chat_id в /var/lib/cigarday-bot/users/ (0600), вне
репозитория: репозиторий публичный. /stop удаляет файл пользователя.

На этом шаге НЕТ рассылки, оплаты, доставки и СБИС. 15-ФЗ: никаких скидок,
акций, призывов купить; бот не продаёт — бронь лишь передаёт менеджеру
просьбу связаться, покупка в магазине (решение владельца 03.11.2026; тексты
показать юристу CigarDay).

Брони приходят в чат CIGARDAY_MANAGER_CHAT (chat_id; менеджер должен хоть
раз нажать Start у бота).

  python3 cigarday/bot.py            # работать (нужен токен)
"""
import datetime
import html
import http.client
import json
import os
import pathlib
import re
import socket
import sys
import time
import traceback
import uuid

import paths
import reco as R
import reco_match as M
import reco_text as T

TOKEN = os.environ.get("CIGARDAY_TESTBOT_TOKEN", "").strip()
STATE = pathlib.Path(os.environ.get("CIGARDAY_BOT_STATE", "/var/lib/cigarday-bot"))
USERS = STATE / "users"
PHOTOS = STATE / "photos.json"
CARDS = paths.REPO / "data" / "cards"
EMPTY_CARD = CARDS / "_empty.jpg"
RATE_MAX = 15
MANAGER_CHAT = os.environ.get("CIGARDAY_MANAGER_CHAT", "").strip()
BOOK_REPEAT_H = 12          # повторная бронь той же сигары — не чаще
API_HOST = "api.telegram.org"
CONNECT_TIMEOUT = 3
POLL_TIMEOUT = 25
# Повтор после того, как запрос уже ушёл, безопасен только там, где второй
# раз ничего не испортит. sendMessage/sendPhoto сюда не входят — иначе
# возможен дубль сообщения.
SAFE_RETRY = {"getUpdates", "getMe", "answerCallbackQuery", "editMessageMedia",
              "editMessageCaption", "editMessageReplyMarkup", "setMyCommands", "deleteMessage"}


def log(*a):
    print(datetime.datetime.now().isoformat(timespec="seconds"), *a, flush=True)


# ── Telegram: постоянное соединение по IPv6 ────────────────────────────

class _HTTPS6(http.client.HTTPSConnection):
    """HTTPS только по IPv6 и с коротким таймаутом на установку соединения."""

    def connect(self):
        last = OSError("нет IPv6-адреса")
        for af, st, pr, _, sa in socket.getaddrinfo(self.host, self.port, socket.AF_INET6, socket.SOCK_STREAM):
            s = socket.socket(af, st, pr)
            s.settimeout(CONNECT_TIMEOUT)
            try:
                s.connect(sa)
                self.sock = self._context.wrap_socket(s, server_hostname=self.host)
                return
            except OSError as e:
                last = e
                s.close()
        raise last


class Telegram:
    def __init__(self, token):
        self.token = token
        self.conn = None

    def close(self):
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = None

    @staticmethod
    def _multipart(params, files):
        b = uuid.uuid4().hex
        out = []
        for k, v in params.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            out += [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode(),
                    str(v).encode(), b"\r\n"]
        for k, path in files.items():
            out += [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"; filename=\"{k}.jpg\"\r\n"
                    f"Content-Type: image/jpeg\r\n\r\n".encode(), pathlib.Path(path).read_bytes(), b"\r\n"]
        out.append(f"--{b}--\r\n".encode())
        return b"".join(out), f"multipart/form-data; boundary={b}"

    def call(self, method, files=None, read_timeout=30, **params):
        if files:
            body, ctype = self._multipart(params, files)
        else:
            body, ctype = json.dumps(params, ensure_ascii=False).encode(), "application/json"
        for attempt in range(4):
            sent = False
            try:
                if self.conn is None:
                    self.conn = _HTTPS6(API_HOST, timeout=CONNECT_TIMEOUT)
                    self.conn.connect()
                self.conn.sock.settimeout(read_timeout)
                self.conn.request("POST", f"/bot{self.token}/{method}", body,
                                  {"Content-Type": ctype, "Connection": "keep-alive"})
                sent = True
                out = json.loads(self.conn.getresponse().read())
            except (OSError, http.client.HTTPException, ValueError) as e:
                self.close()
                # Сервер закрыл простаивающее соединение — запрос до него не
                # дошёл, повторять можно всегда.
                stale = isinstance(e, (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError))
                if sent and not stale and method not in SAFE_RETRY:
                    log(f"tg {method}: ответ не дождались ({type(e).__name__}), не повторяю, чтобы не задвоить")
                    return None
                if attempt == 3:
                    log(f"tg {method}: не вышло за 4 попытки ({type(e).__name__}: {e})")
                time.sleep(0.2)
                continue
            if out.get("error_code") == 429 and attempt < 3:
                time.sleep(min(int((out.get("parameters") or {}).get("retry_after", 1)), 30) + 0.5)
                continue
            if not out.get("ok") and "not modified" not in (out.get("description") or ""):
                log(f"tg {method}: {out.get('description')}")
            return out
        return None


TG = Telegram(TOKEN)


def tg(method, **params):
    return TG.call(method, **params)


# ── фото карточек: загружаем один раз, дальше по file_id ───────────────

_photo_ids = None


def _photos():
    global _photo_ids
    if _photo_ids is None:
        try:
            _photo_ids = json.loads(PHOTOS.read_text())
        except (FileNotFoundError, ValueError):
            _photo_ids = {}
    return _photo_ids


def _remember_photo(key, path, resp):
    try:
        msg = resp["result"]
        fid = max(msg["photo"], key=lambda p: p.get("file_size", 0))["file_id"]
    except (TypeError, KeyError, ValueError):
        return
    ids = _photos()
    ids[key] = {"id": fid, "mtime": int(path.stat().st_mtime)}
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = PHOTOS.with_suffix(".tmp")
    tmp.write_text(json.dumps(ids))
    os.replace(tmp, PHOTOS)


def _card_path(cid):
    p = CARDS / f"{cid}.jpg"
    return p if p.exists() else EMPTY_CARD


def _cached(key, path):
    x = _photos().get(key)
    return x["id"] if x and x.get("mtime") == int(path.stat().st_mtime) else None


def send_card(chat, cid, caption, kb):
    path = _card_path(cid)
    key = path.stem
    fid = _cached(key, path)
    p = {"chat_id": chat, "caption": caption[:1024], "parse_mode": "HTML",
         "reply_markup": {"inline_keyboard": kb}}
    if fid:
        r = tg("sendPhoto", photo=fid, **p)
        if r and r.get("ok"):
            return r
    r = TG.call("sendPhoto", files={"photo": path}, **p)
    if r and r.get("ok"):
        _remember_photo(key, path, r)
        return r
    return send(chat, caption, kb)          # без фото, но карточка не теряется


def edit_card(chat, mid, cid, caption, kb):
    path = _card_path(cid)
    key = path.stem
    fid = _cached(key, path)
    media = {"type": "photo", "caption": caption[:1024], "parse_mode": "HTML"}
    p = {"chat_id": chat, "message_id": mid, "reply_markup": {"inline_keyboard": kb}}
    if fid:
        r = tg("editMessageMedia", media={**media, "media": fid}, **p)
        if r and (r.get("ok") or "not modified" in (r.get("description") or "")):
            return r
    r = TG.call("editMessageMedia", files={"card": path}, media={**media, "media": "attach://card"}, **p)
    if r and r.get("ok"):
        _remember_photo(key, path, r)
    return r


def send(chat, text, kb=None, reply_kb=None):
    p = {"chat_id": chat, "text": text, "parse_mode": "HTML",
         "link_preview_options": {"is_disabled": True}}
    if kb:
        p["reply_markup"] = {"inline_keyboard": kb}
    elif reply_kb:
        p["reply_markup"] = reply_kb
    return tg("sendMessage", **p)


def answer(q, text=None):
    p = {"callback_query_id": q["id"]}
    if text:
        p["text"] = text
    tg("answerCallbackQuery", **p)


# ── хранилище ──────────────────────────────────────────────────────────

def _upath(chat):
    return USERS / f"{int(chat)}.json"


def load_user(chat):
    try:
        u = json.loads(_upath(chat).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    # Оценки — только целые 1–5: файл мог остаться от старой версии бота.
    u["ratings"] = {k: v for k, v in (u.get("ratings") or {}).items()
                    if isinstance(v, int) and 1 <= v <= 5}
    return u


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

_cat = {"mtime": None}


def catalog():
    """Каталог перечитываем, когда ночной сбор или дневная перепроверка
    наличия обновили prices.json — иначе бот показывал бы вчерашнее."""
    mt = (paths.PRICES.stat().st_mtime, paths.PROFILES.stat().st_mtime)
    if _cat["mtime"] != mt:
        prices = json.loads(paths.PRICES.read_text(encoding="utf-8"))
        prof = json.loads(paths.PROFILES.read_text(encoding="utf-8"))
        cat = R.build_catalog(prices, prof)
        _cat.update(mtime=mt, catalog=cat, aliases=M.load_aliases(), idf=R.note_idf(cat))
        log(f"каталог перечитан: в наличии у CigarDay {sum(1 for x in cat.values() if x['in_stock'])}")
    return _cat["catalog"], _cat["aliases"], _cat["idf"]


# ── тексты и кнопки ────────────────────────────────────────────────────

BTN_PICK, BTN_RATE, BTN_LOAD, BTN_HELP = "🎯 Подборка", "⭐ Оценить покупки", "📥 Загрузить историю", "❓ Помощь"
MENU_KB = {"keyboard": [[{"text": BTN_PICK}, {"text": BTN_RATE}],
                        [{"text": BTN_LOAD}, {"text": BTN_HELP}]],
           "resize_keyboard": True, "is_persistent": True,
           "input_field_placeholder": "Меню — кнопки ниже"}
BTN_CANCEL = "Отмена"
CONTACT_KB = {"keyboard": [[{"text": "📱 Поделиться номером", "request_contact": True}],
                           [{"text": BTN_CANCEL}]],
              "resize_keyboard": True, "one_time_keyboard": True,
              "input_field_placeholder": "Нажмите «Поделиться номером»"}

HELLO = ("<b>Здравствуйте!</b>\n\n"
         "Это бот-подсказчик CigarDay. Он смотрит, что вы уже курили и как это оценили, "
         "и подбирает сигары по вашему вкусу — из того, что сейчас есть в наличии.\n\n"
         "<blockquote>Для подбора бот хранит историю ваших покупок и ваши оценки. "
         "Если вы бронируете сигару — ещё номер телефона, чтобы менеджер CigarDay мог с вами связаться. "
         "Удалить всё можно в любой момент командой /stop. Нажимая «Согласен», вы соглашаетесь "
         "на обработку этих данных (152-ФЗ).</blockquote>")

ONBOARD = ("<b>С чего начнём?</b>\n\n"
           "Для подбора нужна история покупок. В рабочей версии она будет подтягиваться из CigarDay сама, "
           "а пока возьмите готовый пример или загрузите свою.")

KB_ONBOARD = [[{"text": "🇨🇺 Кубинская классика", "callback_data": "demo:demo1"},
               {"text": "🌎 Новый Свет, покрепче", "callback_data": "demo:demo2"}],
              [{"text": "📥 Загрузить свою историю", "callback_data": "load"}]]

HELP = ("<b>Как пользоваться</b>\n\n"
        "🎯 <b>Подборка</b> — ваши любимые сигары, которые сейчас есть в наличии, и три похожие "
        "по вкусу, крепости и формату — около вашей обычной цены. Листайте стрелками ◀ ▶.\n\n"
        "⭐ <b>Оценить покупки</b> — звёзды от 1 до 5. Пятёрки и четвёрки подборка учитывает сильнее, "
        "единицы и двойки — убирает и не предлагает похожие. Оценку можно поменять.\n\n"
        "📌 <b>Забронировать</b> — кнопка на карточке: менеджер CigarDay свяжется с вами, "
        "покупка — в магазине. 🔗 <b>На сайте</b> — подробнее о сигаре на cigarday.ru.\n\n"
        "📥 <b>Загрузить историю</b> — свои покупки текстом.\n\n"
        "/stop — удалить все ваши данные.")

LOAD_HELP = ("<b>Загрузка истории</b>\n\n"
             "Пришлите покупки одним сообщением — по строке на покупку:\n"
             "<code>название; дата; количество; цена за штуку</code>\n\n"
             "<blockquote>Partagas Serie D No. 4; 14.10.2026; 5; 2750\n"
             "Montecristo No. 2; 20.09.2026; 3; 3250</blockquote>\n"
             "Количество и цену можно не указывать. Загруженная история заменит прежнюю.")


def plural(n, one, few, many):
    """1 покупка, 2 покупки, 5 покупок."""
    n10, n100 = n % 10, n % 100
    w = one if n10 == 1 and n100 != 11 else few if 2 <= n10 <= 4 and not 12 <= n100 <= 14 else many
    return f"{n} {w}"


def _btn(text, data):
    return {"text": text, "callback_data": data}


def shop_url(it):
    """Страница сигары на cigarday.ru — оформление там, по правилам магазина;
    сам бот заказов не принимает. Только https://cigarday.ru/…"""
    u = (it or {}).get("url") or ""
    return u if re.fullmatch(r"https://(www\.)?cigarday\.ru/[\w\-/%.]*", u) else None


# ── сценарии ───────────────────────────────────────────────────────────

def ask_consent(chat):
    send(chat, HELLO, [[_btn("Согласен", "consent")]])


def do_load(chat, u, text, source, title=None):
    _, aliases, _ = catalog()
    rows, unknown = M.parse_history(text, aliases)
    if not rows:
        miss = ("\n\nНе узнал: " + html.escape("; ".join(x["name"] for x in unknown[:10]))) if unknown else ""
        send(chat, "Не узнал ни одной сигары." + miss + "\n\n" + LOAD_HELP)
        return
    u.update(history=rows, ratings={}, source=source, awaiting_load=False, pick=None,
             loaded_at=datetime.date.today().isoformat())
    save_user(chat, u)
    uniq = len(set(r["cid"] for r in rows))
    head = f"Пример загружен: <b>{html.escape(title)}</b>" if title else "<b>История загружена</b>"
    msg = [head, f"{plural(len(rows), 'покупка', 'покупки', 'покупок')}, "
                 f"{plural(uniq, 'сигара', 'разные сигары', 'разных сигар')}."]
    if unknown:
        msg.append("<i>Не узнал и не учитываю: " + html.escape("; ".join(x["name"] for x in unknown[:10])) + "</i>")
    msg.append("\nОцените покупки — так подборка станет точнее. Или сразу посмотрите, что получилось.")
    send(chat, "\n".join(msg), [[_btn("⭐ Оценить покупки", "rv:0"), _btn("🎯 Подборка", "pnew")]])


def need_history(chat, u):
    if u.get("history"):
        return False
    send(chat, ONBOARD, KB_ONBOARD)
    return True


# Подборка-карусель ─────────────────────────────────────────────────────

def compute_pick(u):
    cat, _, _ = catalog()
    res = R.recommend(u["history"], u.get("ratings") or {}, cat)
    pk = {"f": [{"cid": f["item"]["cid"], "why": f["reason"], "rating": f["rating"]} for f in res["favorites"]],
          "s": [{"cid": s["item"]["cid"], "why": s["reason"],
                 "anchor": (cat.get(s["anchor"]) or {}).get("name")} for s in res["suggestions"]],
          "absent": [x["name"] for x in res["favorites_absent"]],
          "price": res.get("price"), "at": T.today_ru()}
    u["pick"] = pk
    return pk


def pick_slide(u, tab, i):
    """(cid для фото, подпись, клавиатура)."""
    cat, _, idf = catalog()
    pk = u["pick"]
    items = pk[tab]
    nf, ns = len(pk["f"]), len(pk["s"])
    tabs = [_btn(("● " if tab == "s" else "") + f"✨ Вам понравится · {ns}", "p:s:0"),
            _btn(("● " if tab == "f" else "") + f"❤️ Любимые · {nf}", "p:f:0")]
    kb = [tabs]
    if not items:
        if tab == "f":
            cap = ("<b>Ваши любимые</b>\n\nСейчас ни одной из них нет в наличии"
                   + (": " + html.escape(", ".join(pk["absent"])) if pk["absent"] else "") + ".")
        else:
            cap = ("<b>Вам понравится</b>\n\nПока не из чего подобрать. Оцените покупки "
                   "или загрузите историю побольше.")
        kb.append([_btn("⭐ Оценить покупки", "rv:0"), _btn("↻ Обновить", "pnew")])
        return None, cap, kb
    i %= len(items)
    x = items[i]
    it = cat.get(x["cid"])
    if not it:
        return None, "Эта сигара пропала из каталога — обновите подборку.", kb + [[_btn("↻ Обновить", "pnew")]]
    note = None
    if tab == "f" and i == len(items) - 1 and pk["absent"]:
        note = "Сейчас нет в наличии: " + ", ".join(pk["absent"])
    head = "❤️ Ваши любимые — есть в наличии" if tab == "f" else "✨ Вам может понравиться"
    cap = f"<i>{head}</i>\n\n" + T.card(it, x["why"], idf, anchor_name=x.get("anchor"),
                                        rating=x.get("rating"), note=note)
    if len(items) > 1:
        kb.append([_btn("◀", f"p:{tab}:{(i - 1) % len(items)}"),
                   _btn(f"{i + 1} / {len(items)}", "noop"),
                   _btn("▶", f"p:{tab}:{(i + 1) % len(items)}")])
    row = [_btn("📌 Забронировать", f"bk:{tab}:{i}")]
    url = shop_url(it)
    if url:
        row.append({"text": "🔗 На сайте", "url": url})
    kb.append(row)
    kb.append([_btn("⭐ Оценить покупки", "rv:0"), _btn("↻ Обновить", "pnew")])
    return x["cid"], cap, kb


def show_pick(chat, u, tab=None, i=0, mid=None, fresh=False):
    if need_history(chat, u):
        return
    if fresh or not u.get("pick"):
        compute_pick(u)
        save_user(chat, u)
    pk = u["pick"]
    if tab is None:
        tab = "s" if pk["s"] else "f"
    cid, cap, kb = pick_slide(u, tab, i)
    if mid:
        edit_card(chat, mid, cid or "_empty", cap, kb)
    else:
        send_card(chat, cid or "_empty", cap, kb)


# Оценка покупок — по одной ────────────────────────────────────────────

def rate_order(u):
    st = R.purchase_stats(u["history"])
    return sorted(st, key=lambda c: st[c]["last"] or datetime.date.min, reverse=True)[:RATE_MAX]


def rate_slide(u, i, done=False):
    cat, _, _ = catalog()
    order = u.get("rate_order") or rate_order(u)
    i %= len(order)
    cid = order[i]
    st = R.purchase_stats(u["history"])[cid]
    cur = (u.get("ratings") or {}).get(cid)
    cap = T.rate_card(cat[cid], st, cur, i + 1, len(order))
    if done:
        n = sum(1 for c in order if (u.get("ratings") or {}).get(c))
        cap += f"\n\n✅ <b>Оценено {n} из {len(order)}.</b> Подборка уже учитывает ваши оценки."
    stars = [_btn("★" if cur and k <= cur else "☆", f"r:{i}:{k}") for k in range(1, 6)]
    kb = [stars]
    if len(order) > 1:
        kb.append([_btn("◀", f"rv:{(i - 1) % len(order)}"), _btn(f"{i + 1} / {len(order)}", "noop"),
                   _btn("▶", f"rv:{(i + 1) % len(order)}")])
    kb.append([_btn("🎯 Показать подборку", "pnew")])
    return cid, cap, kb


def show_rate(chat, u, i=0, mid=None, done=False):
    if need_history(chat, u):
        return
    if not u.get("rate_order") or set(u["rate_order"]) - {h["cid"] for h in u["history"]}:
        u["rate_order"] = rate_order(u)
        save_user(chat, u)
    cid, cap, kb = rate_slide(u, i, done)
    if mid:
        edit_card(chat, mid, cid, cap, kb)
    else:
        send_card(chat, cid, cap, kb)


# ── обработчики ────────────────────────────────────────────────────────

# ── бронь ──────────────────────────────────────────────────────────────

def _who(frm):
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "клиент"
    return name, frm.get("username")


def book(chat, u, cid, frm):
    """Отправить заявку менеджеру. Номер уже должен быть в u["phone"]."""
    cat, _, _ = catalog()
    it = cat.get(cid)
    if not it or not it["in_stock"]:
        send(chat, "Эта сигара уже закончилась — обновите подборку.", reply_kb=MENU_KB)
        return
    name, uname = _who(frm)
    e = html.escape
    url = shop_url(it)
    msg = ("📌 <b>Бронь из бота</b>\n\n"
           f"<b>{e(it['name'])}</b> — {e(T.rub(it['price']))} за штуку"
           + (f"\n{e(url)}" if url else "")
           + f"\n\nКлиент: <a href=\"tg://user?id={int(chat)}\">{e(name)}</a>"
           + (f" (@{e(uname)})" if uname else "")
           + f"\nТелефон: <code>{e(u.get('phone') or '—')}</code>")
    r = send(int(MANAGER_CHAT), msg) if MANAGER_CHAT.lstrip("-").isdigit() else None
    if not (r and r.get("ok")):
        log(f"бронь {cid}: не доставлена менеджеру (CIGARDAY_MANAGER_CHAT не задан или недоступен)")
        send(chat, "Не получилось передать заявку менеджеру — попробуйте чуть позже "
                   "или позвоните в магазин: +7 (812) 317-22-98.", reply_kb=MENU_KB)
        return
    u.setdefault("bookings", []).append({"cid": cid, "at": datetime.datetime.now().isoformat(timespec="seconds")})
    u["bookings"] = u["bookings"][-50:]
    u.pop("book_pending", None)
    save_user(chat, u)
    log(f"бронь {cid}: передана менеджеру")
    send(chat, f"✅ <b>Заявка принята: {e(it['name'])}</b>\n\n"
               f"Менеджер CigarDay свяжется с вами по номеру <code>{e(u['phone'])}</code>, "
               "чтобы договориться, когда вам удобно забрать. Покупка — в магазине.",
         reply_kb=MENU_KB)


def start_booking(chat, u, cid, frm, q):
    last = next((b for b in reversed(u.get("bookings") or []) if b["cid"] == cid), None)
    if last and datetime.datetime.now() - datetime.datetime.fromisoformat(last["at"]) \
            < datetime.timedelta(hours=BOOK_REPEAT_H):
        answer(q, "Заявка на эту сигару уже у менеджера")
        return
    answer(q)
    if u.get("phone"):
        book(chat, u, cid, frm)
        return
    u["book_pending"] = cid
    save_user(chat, u)
    it = catalog()[0].get(cid) or {}
    send(chat, f"📌 <b>Бронь: {html.escape(it.get('name', ''))}</b>\n\n"
               "Чтобы менеджер CigarDay мог с вами связаться, поделитесь номером телефона — "
               "кнопка внизу. Номер увидит только менеджер; удалить — /stop.",
         reply_kb=CONTACT_KB)


def on_message(m):
    chat = m["chat"]["id"]
    if m["chat"].get("type") != "private":
        return
    text = (m.get("text") or "").strip()
    first, _, rest = text.partition("\n")
    cmd, _, arg = first.partition(" ") if first.startswith("/") else ("", "", "")
    cmd = cmd.split("@")[0].lower()
    rest = (arg + "\n" + rest).strip() if cmd else text
    cmd = {BTN_PICK: "/pick", BTN_RATE: "/rate", BTN_LOAD: "/load", BTN_HELP: "/help"}.get(text, cmd)
    u = load_user(chat)

    if cmd == "/stop":
        had = delete_user(chat)
        send(chat, "Готово: история покупок, оценки и согласие удалены. Вернуться — /start."
             if had else "У меня нет ваших данных.", reply_kb={"remove_keyboard": True})
        return
    if not u.get("consent"):
        ask_consent(chat)
        return
    if m.get("contact") and u.get("book_pending"):
        c = m["contact"]
        if c.get("user_id") != (m.get("from") or {}).get("id"):
            send(chat, "Нужен ваш собственный номер — нажмите «📱 Поделиться номером».", reply_kb=CONTACT_KB)
            return
        phone = re.sub(r"[^\d+]", "", c.get("phone_number") or "")[:20]
        u["phone"] = phone if phone.startswith("+") else "+" + phone
        book(chat, u, u["book_pending"], m.get("from") or {})
        return
    if u.get("book_pending") and (text == BTN_CANCEL or cmd):
        u.pop("book_pending", None)
        save_user(chat, u)
        if text == BTN_CANCEL:
            send(chat, "Бронь отменена.", reply_kb=MENU_KB)
            return
    if cmd == "/start":
        send(chat, "Меню — внизу экрана.", reply_kb=MENU_KB)
        if not u.get("history"):
            send(chat, ONBOARD, KB_ONBOARD)
        return
    if cmd in ("/demo1", "/demo2"):
        title, demo = T.DEMOS[cmd[1:]]
        do_load(chat, u, demo, cmd[1:], title)
    elif cmd == "/load":
        if rest and rest != text:
            do_load(chat, u, rest, "load")
        else:
            u["awaiting_load"] = True
            save_user(chat, u)
            send(chat, LOAD_HELP)
    elif cmd == "/rate":
        show_rate(chat, u)
    elif cmd == "/pick":
        show_pick(chat, u, fresh=True)
    elif cmd in ("/help", "/menu"):
        send(chat, HELP, KB_ONBOARD if not u.get("history") else
             [[_btn("🇨🇺 Пример: кубинская классика", "demo:demo1")],
              [_btn("🌎 Пример: Новый Свет", "demo:demo2")]])
    elif not cmd and u.get("awaiting_load"):
        do_load(chat, u, text, "load")
    else:
        send(chat, "Выберите в меню внизу 👇", reply_kb=MENU_KB)


# callback_data присылает клиент, и подделать его можно: разбираем строго,
# по белому списку форм, а числа держим в узких пределах.
_CB = [
    (re.compile(r"consent|noop|load|pnew"), lambda m: (m[0],)),
    (re.compile(r"demo:(demo1|demo2)"), lambda m: ("demo", m[1])),
    (re.compile(r"p:([fs]):(\d{1,2})"), lambda m: ("p", m[1], int(m[2]))),
    (re.compile(r"rv:(\d{1,2})"), lambda m: ("rv", int(m[1]))),
    (re.compile(r"bk:([fs]):(\d{1,2})"), lambda m: ("bk", m[1], int(m[2]))),
    (re.compile(r"r:(\d{1,2}):([1-5])"), lambda m: ("r", int(m[1]), int(m[2]))),
]


def parse_cb(data):
    for rx, conv in _CB:
        m = rx.fullmatch(data or "")
        if m:
            return conv(m)
    return None


def on_callback(q):
    msg = q.get("message") or {}
    chat = msg["chat"]["id"]
    mid = msg.get("message_id")
    is_photo = bool(msg.get("photo"))
    cb = parse_cb(q.get("data"))
    if cb is None:
        answer(q)
        return
    data = cb[0]
    u = load_user(chat)

    if data == "consent":
        u["consent"] = datetime.datetime.now().isoformat(timespec="seconds")
        save_user(chat, u)
        answer(q, "Спасибо!")
        tg("editMessageReplyMarkup", chat_id=chat, message_id=mid, reply_markup={"inline_keyboard": []})
        send(chat, "Спасибо! Меню — внизу экрана.", reply_kb=MENU_KB)
        send(chat, ONBOARD, KB_ONBOARD)
        return
    if not u.get("consent"):
        answer(q, "Сначала нажмите /start")
        return
    if data == "noop":
        answer(q)
    elif data == "demo":
        answer(q, "Загружаю пример")
        title, demo = T.DEMOS[cb[1]]
        do_load(chat, u, demo, cb[1], title)
    elif data == "load":
        answer(q)
        u["awaiting_load"] = True
        save_user(chat, u)
        send(chat, LOAD_HELP)
    elif data == "pnew":
        answer(q, "Подбираю…")
        show_pick(chat, u, fresh=True, mid=mid if is_photo else None)
    elif data == "p":
        _, tab, i = cb
        answer(q)
        if not u.get("pick"):
            show_pick(chat, u, fresh=True, mid=mid if is_photo else None)
        else:
            show_pick(chat, u, tab=tab, i=i, mid=mid if is_photo else None)
    elif data == "bk":
        _, tab, i = cb
        items = (u.get("pick") or {}).get(tab) or []
        if i >= len(items):
            answer(q, "Подборка обновилась — откройте её заново")
            return
        start_booking(chat, u, items[i]["cid"], q.get("from") or {}, q)
    elif data == "rv":
        answer(q)
        show_rate(chat, u, cb[1], mid=mid if is_photo else None)
    elif data == "r":
        _, i, n = cb
        order = u.get("rate_order") or []
        if not order or i >= len(order) or order[i] not in {h["cid"] for h in u.get("history") or []}:
            answer(q, "История изменилась — откройте оценку заново")
            return
        u.setdefault("ratings", {})[order[i]] = n
        u["pick"] = None                     # подборка пересчитается с новой оценкой
        save_user(chat, u)
        answer(q, f"Сохранено: {'★' * n}{'☆' * (5 - n)}")
        # Дальше — к следующей неоценённой; оценены все — остаёмся с итогом.
        rated = u["ratings"]
        nxt = next((j for j in list(range(i + 1, len(order))) + list(range(0, i)) if not rated.get(order[j])), None)
        if nxt is None:
            show_rate(chat, u, i, mid=mid if is_photo else None, done=True)
        else:
            show_rate(chat, u, nxt, mid=mid if is_photo else None)
    else:
        answer(q)


def poll():
    off = load_offset()
    r = TG.call("getUpdates", read_timeout=POLL_TIMEOUT + 10, offset=off + 1, timeout=POLL_TIMEOUT,
                allowed_updates=["message", "callback_query"])
    if not r or not r.get("ok"):
        time.sleep(2)
        return
    for upd in r.get("result", []):
        # offset сохраняем ДО разбора: одно сломанное сообщение не должно
        # повторяться бесконечно и стопорить бота всем остальным.
        off = max(off, upd["update_id"])
        save_offset(off)
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
        {"command": "load", "description": "Загрузить свою историю"},
        {"command": "help", "description": "Как пользоваться"},
        {"command": "stop", "description": "Удалить мои данные"},
    ])
    log("бот запущен")
    while True:
        poll()


if __name__ == "__main__":
    sys.exit(main())
