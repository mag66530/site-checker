"""
Имя теста (плейсхолдеры в ИМЯ и ФОРМАТ_*) и единая интерпретация «включено» из config.

Используется test_all.run_test и config_editor (чтобы чекбоксы в UI совпадали с прогоном).
"""


def cfg_enabled(v, default: bool = True) -> bool:
    """
    Надёжное bool для полей «включено» из dict / ast / ручного редактирования.

    - bool(False) → выкл
    - None → default (обычно вкл)
    - строки "false", "0", "нет", "off", "выкл" → выкл
    - строка "False" (типичная ошибка) → выкл, не True как у bool("False")
    """
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v != 0
    s = str(v).strip().lower()
    if s in (
        "",
        "0",
        "false",
        "no",
        "off",
        "нет",
        "н",
        "выкл",
        "disabled",
    ):
        return False
    if s in ("1", "true", "yes", "on", "да", "вкл", "enabled"):
        return True
    # неизвестная непустая строка – не считаем «включено» по ошибке true из bool("…")
    return default


def _replace_название_aliases(out: str, nv: str) -> str:
    """Только {название} (нижний регистр)."""
    return out.replace("{название}", nv)


def expand_fragment_placeholders(frag: str, ctx: dict) -> str:
    """Плейсхолдеры внутри ИМЯ (без {имя}): дата, страница, значение, название и т.д."""
    out = str(frag or "")
    nv = str(ctx.get("название", ""))
    for _ in range(12):
        prev = out
        for key in ("значение", "страница", "время", "дата"):
            out = out.replace("{" + key + "}", str(ctx.get(key, "")))
        out = _replace_название_aliases(out, nv)
        out = out.replace("{value}", str(ctx.get("значение", "")))
        out = out.replace("{page}", str(ctx.get("страница", "")))
        out = out.replace("{date}", str(ctx.get("дата", "")))
        out = out.replace("{time}", str(ctx.get("время", "")))
        if out == prev:
            break
    return out


def apply_name_format_template(tpl: str, ctx: dict) -> str:
    """Подстановка {имя}, {страница}, {название}, … без str.format."""
    out = str(tpl or "")
    nv = str(ctx.get("название", ""))
    for _ in range(12):
        prev = out
        for key in ("значение", "страница", "время", "дата", "имя"):
            out = out.replace("{" + key + "}", str(ctx.get(key, "")))
        out = _replace_название_aliases(out, nv)
        out = out.replace("{value}", str(ctx.get("значение", "")))
        out = out.replace("{name}", str(ctx.get("имя", "")))
        out = out.replace("{page}", str(ctx.get("страница", "")))
        out = out.replace("{date}", str(ctx.get("дата", "")))
        out = out.replace("{time}", str(ctx.get("время", "")))
        if out == prev:
            break
    return out


def build_test_name(
    *,
    имя_конфига: str,
    название_из_конфига: str | None,
    страница: str,
    значение_авто: str,
    формат_если_имя: str,
    формат_если_авто: str,
    дата: str,
    время: str,
    название_для_плейсхолдеров: str | None = None,
) -> str:
    """
    Итоговая строка имени теста для лога и полей формы.

    По умолчанию формат «{имя}» – в поле попадает то, что задано в ИМЯ (после плейсхолдеров),
    без автоматического хвоста «. страница . дата»; при необходимости задайте в config.py
    ФОРМАТ_ИМЕНИ_ТЕСТА = "{имя}. {страница}. {дата}" и т.п.
    """
    _авто = "" if значение_авто is None else str(значение_авто).strip()
    effective_имя = (имя_конфига or "").strip()

    # Подстановка {название} в шаблоне ИМЯ (см. название_для_плейсхолдеров)
    nv = название_для_плейсхолдеров
    if nv is None:
        nv = название_из_конфига
    nv = "" if nv is None else str(nv).strip()

    ctx_partial = {
        "страница": (страница or "").strip(),
        "дата": дата,
        "время": время,
        "значение": _авто,
        "название": nv,
    }
    effective_имя = expand_fragment_placeholders(effective_имя, ctx_partial)

    ctx = {
        "имя": effective_имя,
        "страница": ctx_partial["страница"],
        "дата": дата,
        "время": время,
        "значение": _авто,
        "название": nv,
    }
    try:
        fmt_test = (формат_если_имя or "").strip() or "{имя}"
        fmt_auto = (формат_если_авто or "").strip() or "{значение}"
        if ctx["имя"]:
            return apply_name_format_template(fmt_test, ctx)
        return apply_name_format_template(fmt_auto, ctx)
    except Exception as e:
        print(f"   ⚠️ Ошибка формата имени теста: {e}")
        if ctx["имя"]:
            return f"{ctx['имя']}. {ctx['страница']}. {ctx['дата']}"
        return f"{ctx['значение']}. {ctx['страница']}. {ctx['дата']}"
