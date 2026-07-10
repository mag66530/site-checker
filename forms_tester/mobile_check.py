"""
Мобильная вёрстка форм (пункты чек-листа):
  • «Элементы формы не выходят за границы экрана» - при ширине телефона нет
    горизонтального скролла (ничего не торчит за правый край).
  • «Кнопки и поля формы удобны для нажатия на тач-скринах» - интерактивные
    элементы не мельче ~44px (рекомендация Apple/Google для тач-целей).

Открываем каждую страницу форм СВЕЖИМ МОБИЛЬНЫМ контекстом (ширина телефона,
touch) и меряем геометрию - ничего не заполняем и не отправляем. Модалку
(«Обратный звонок» и т.п.) пытаемся открыть по кнопке-опенеру, чтобы измерить
и её поля. Вызывается из forms_run после прогона форм. Пишет строки в «Логи».
"""
import os
from datetime import datetime

_MOBILE_W = 390          # ширина экрана телефона (iPhone 12/13/14)
_MOBILE_H = 844
_TAP_MIN = 44            # минимальная сторона тач-цели (px)
_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
              "Mobile/15E148 Safari/604.1")
# Текст кнопок, открывающих модалки-формы (кликаем ТОЛЬКО <button>, не ссылки -
# ссылка увела бы со страницы и испортила замер).
_OPENERS = ("заказать звонок", "обратный звонок", "заказать обратный",
            "оставить заявку", "быстрый заказ", "купить в 1 клик",
            "перезвон", "заказать в 1 клик")


def _playwright_proxy_from_env():
    """Тот же прокси, что и у прогона форм (сайты, режущие прямое подключение)."""
    from urllib.parse import urlparse, unquote
    raw = (os.environ.get("FORMS_PROXY") or "").strip()
    if not raw:
        return None
    pr = urlparse(raw if "://" in raw else "http://" + raw)
    if not pr.hostname:
        return None
    server = f"{pr.scheme or 'http'}://{pr.hostname}" + (f":{pr.port}" if pr.port else "")
    conf = {"server": server}
    if pr.username:
        conf["username"] = unquote(pr.username)
    if pr.password:
        conf["password"] = unquote(pr.password)
    return conf


def _измерить(page) -> dict:
    """{overflow_px, торчат[], мелкие[]} для текущего состояния страницы.
    overflow_px - на сколько страница шире экрана (горизонтальный скролл).
    торчат - элементы форм, вылезающие за правый край. мелкие - тач-цели < 44px."""
    return page.evaluate(
        "(TAP) => {"
        " const W = document.documentElement.clientWidth;"
        " const overflow = Math.max(0, (document.documentElement.scrollWidth||0) - W);"
        " const vis = el => { const r=el.getBoundingClientRect(); const s=getComputedStyle(el);"
        "   return r.width>0 && r.height>0 && s.visibility!=='hidden' && s.display!=='none'"
        "   && s.opacity!=='0'; };"
        " const short = s => (s||'').replace(/\\s+/g,' ').trim().slice(0,26);"
        # элементы форм, торчащие за правый край экрана (не считаем контейнеры шире экрана)
        " const торчат = [];"
        " for (const el of document.querySelectorAll('input,textarea,select,button')) {"
        "   if (!vis(el)) continue; const t=(el.type||'').toLowerCase();"
        "   if (t==='hidden') continue; const r=el.getBoundingClientRect();"
        "   if (r.right > W + 2 && r.width <= W) {"
        "     торчат.push(short((el.name||el.id||el.tagName)+''));"
        "   } }"
        # тач-цели мельче порога: КНОПКИ (<36 в высоту / <44 в ширину) и ПОЛЯ (<30 в высоту)
        " const мелкие = [];"
        " for (const el of document.querySelectorAll("
        "   'form button, form input[type=submit], form input[type=button], button[type=submit],"
        "    .modal button, [class*=popup] button, [class*=callme] button, [id*=callme] button')) {"
        "   if (!vis(el)) continue; const r=el.getBoundingClientRect();"
        "   if (r.height>0 && (r.height < 36 || r.width < TAP)) {"
        "     const lbl=short(el.innerText||el.value||el.getAttribute('aria-label')||'кнопка');"
        "     мелкие.push(`${lbl||'кнопка'} (${Math.round(r.width)}×${Math.round(r.height)})`);"
        "   } }"
        " for (const el of document.querySelectorAll("
        "   'form input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio]),"
        "    form textarea, form select,"
        "    .modal input:not([type=hidden]), [id*=callme] input:not([type=hidden])')) {"
        "   if (!vis(el)) continue; const r=el.getBoundingClientRect();"
        "   if (r.height>0 && r.height < 30) {"
        "     const lbl=short(el.name||el.placeholder||el.id||'поле');"
        "     мелкие.push(`${lbl||'поле'} (высота ${Math.round(r.height)})`);"
        "   } }"
        " return {overflow, торчат:[...new Set(торчат)].slice(0,8), мелкие:[...new Set(мелкие)].slice(0,8)};"
        "}", _TAP_MIN)


# Ширины экрана для проверки «на всех устройствах».
_ШИРИНЫ = [(360, "телефон"), (768, "планшет"), (1280, "десктоп")]


def проверить_адаптивность(page) -> dict:
    """Меняет ширину экрана (телефон/планшет/десктоп) и ищет ПРИЗНАКИ СЛОМАННОЙ
    вёрстки формы: горизонтальный скролл, перекрытие элементов формы, форма стала
    непригодной (не видно поля/кнопки). Возвращает {ок, детали[]}. Это объективное
    ядро пункта «корректно на всех устройствах» - «красоту/макет» видит человек."""
    проблемы = []
    for w, имя in _ШИРИНЫ:
        try:
            page.set_viewport_size({"width": w, "height": 900})
            page.wait_for_timeout(450)
            r = page.evaluate(
                "() => {"
                " const W=document.documentElement.clientWidth;"
                " const overflow=Math.max(0,(document.documentElement.scrollWidth||0)-W);"
                " const vis=el=>{const r=el.getBoundingClientRect();const s=getComputedStyle(el);"
                "   return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};"
                " const sel='form input:not([type=hidden]),form textarea,form select,form button,"
                "[id*=callme] input:not([type=hidden]),[id*=callme] button,.modal input:not([type=hidden]),.modal button';"
                " const els=[...document.querySelectorAll(sel)].filter(vis);"
                " const inter=(a,b)=>{const x=Math.max(0,Math.min(a.right,b.right)-Math.max(a.left,b.left));"
                "   const y=Math.max(0,Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top));return x*y;};"
                " let overlap=0;"
                " for(let i=0;i<els.length;i++)for(let j=i+1;j<els.length;j++){"
                "   const a=els[i].getBoundingClientRect(),b=els[j].getBoundingClientRect();"
                "   const amin=Math.min(a.width*a.height,b.width*b.height);"
                "   if(amin>0 && inter(a,b) > amin*0.35) overlap++; }"
                " const hasBtn=[...document.querySelectorAll('form button,form [type=submit],[id*=callme] button,.modal button')].some(vis);"
                " const hasField=[...document.querySelectorAll('form input:not([type=hidden]),form textarea,[id*=callme] input:not([type=hidden])')].some(vis);"
                " return {overflow, overlap, usable:(hasBtn&&hasField), nforms:document.querySelectorAll('form').length};"
                "}")
            if int(r.get("overflow") or 0) > 8:
                проблемы.append(f"{имя} ({w}px): горизонтальный скролл (+{int(r['overflow'])}px)")
            if int(r.get("overlap") or 0) > 0:
                проблемы.append(f"{имя} ({w}px): элементы формы налезают друг на друга")
            if int(r.get("nforms") or 0) > 0 and not r.get("usable"):
                проблемы.append(f"{имя} ({w}px): форма непригодна (не видно поля или кнопки)")
        except Exception:  # noqa: BLE001
            continue
    return {"ок": not проблемы, "детали": проблемы[:8]}


def _открыть_модалку(page) -> bool:
    """Пытается открыть форму-модалку кликом по кнопке-опенеру. True, если кликнули."""
    try:
        buttons = page.locator("button:visible")
        for i in range(min(buttons.count(), 25)):
            try:
                txt = (buttons.nth(i).inner_text(timeout=500) or "").strip().lower()
            except Exception:  # noqa: BLE001
                continue
            if txt and any(k in txt for k in _OPENERS):
                buttons.nth(i).click(timeout=2500)
                page.wait_for_timeout(900)
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _записать(excel_path, метка, url, название, ок, коммент):
    import test_all as t
    now = datetime.now()
    t.append_log_row(excel_path, {
        "дата": now.strftime("%d.%m.%Y"), "время": now.strftime("%H:%M:%S"),
        "город": метка, "страница": "Мобильная вёрстка", "url": url,
        "название": название,
        "статус": "OK" if ок else "Проверить",
        "комментарий": "" if ок else коммент,
        "код": "mobile",
    })


def выполнить_проверку(страницы, excel_path: str = "log_forms.xlsx",
                       show: bool = False, log=print) -> bool:
    """`страницы` - список (метка, url). По каждой открываем мобильный контекст,
    меряем горизонтальный скролл и тач-размеры (в т.ч. в открытой модалке).
    Пишет 2 строки на страницу. Тихо пропускается без страниц."""
    страницы = [(m, u) for (m, u) in (страницы or []) if u]
    if not страницы:
        return False

    from playwright.sync_api import sync_playwright
    log(f"📱 Мобильная вёрстка: проверяю {len(страницы)} страниц(ы) на ширине {_MOBILE_W}px …")
    with sync_playwright() as pw:
        _kw = dict(headless=not show,
                   args=["--disable-blink-features=AutomationControlled"])
        _prx = _playwright_proxy_from_env()
        if _prx:
            _kw["proxy"] = _prx
        b = pw.chromium.launch(**_kw)
        ctx = b.new_context(locale="ru-RU", user_agent=_MOBILE_UA,
                            viewport={"width": _MOBILE_W, "height": _MOBILE_H},
                            is_mobile=True, has_touch=True, device_scale_factor=2)
        try:
            for метка, url in страницы:
                page = ctx.new_page()
                _ad = {"ок": True, "детали": []}
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(2500)
                    m = _измерить(page)
                    # Модалка (если есть кнопка-опенер) - домеряем её элементы.
                    if _открыть_модалку(page):
                        m2 = _измерить(page)
                        m["overflow"] = max(m["overflow"], m2["overflow"])
                        m["торчат"] = list(dict.fromkeys(m["торчат"] + m2["торчат"]))[:8]
                        m["мелкие"] = list(dict.fromkeys(m["мелкие"] + m2["мелкие"]))[:8]
                    # «На всех устройствах»: несколько ширин + детект поломок вёрстки.
                    # МЕНЯЕТ ширину экрана, поэтому строго ПОСЛЕДНИМ (страница дальше
                    # закрывается, восстанавливать не нужно).
                    _ad = проверить_адаптивность(page)
                except Exception as e:  # noqa: BLE001
                    log(f"   ⚠️ {метка}: не удалось измерить ({str(e)[:70]})")
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass
                    continue
                finally:
                    try:
                        page.close()
                    except Exception:  # noqa: BLE001
                        pass

                # 1) Горизонтальный скролл / элементы за границей экрана.
                _ov = int(m.get("overflow") or 0)
                _ov_ок = _ov <= 5
                _ov_ком = f"Страница шире экрана на {_ov}px - есть горизонтальный скролл"
                if m.get("торчат"):
                    _ov_ком += " (за край выходят: " + ", ".join(m["торчат"]) + ")"
                _ov_ком += "."
                _записать(excel_path, метка, url,
                          "Мобильная вёрстка: нет горизонтального скролла",
                          _ov_ок, _ov_ком)

                # 2) Тач-размер элементов формы (кнопки/поля ≥ ~44px).
                _tap_ок = not m.get("мелкие")
                _tap_ком = ("Мелкие для тача элементы (меньше ~44px): "
                            + ", ".join(m["мелкие"]) + " - на телефоне трудно попасть пальцем.")
                _записать(excel_path, метка, url,
                          "Мобильная вёрстка: тач-размер кнопок/полей",
                          _tap_ок, _tap_ком)

                # 3) Вёрстка на разных устройствах (телефон/планшет/десктоп): без поломок.
                _записать(excel_path, метка, url,
                          "Вёрстка на устройствах (телефон/планшет/десктоп): без поломок",
                          _ad["ок"],
                          "Возможные поломки вёрстки — " + "; ".join(_ad["детали"]) + ".")

                log(f"   {метка}: скролл={'нет' if _ov_ок else f'+{_ov}px'}, "
                    f"мелких тач-целей={len(m.get('мелкие') or [])}, "
                    f"поломок вёрстки={len(_ad.get('детали') or [])}")
        finally:
            b.close()
    log("✅ Мобильная вёрстка проверена - смотри строки «Мобильная вёрстка: …» в «Логах».")
    return True
