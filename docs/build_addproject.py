# -*- coding: utf-8 -*-
"""Отдельный документ «Добавление проекта» — та же вёрстка, что у гайда.

Берёт оформление (стили и помощники) из преамбулы build_guide.py и контент из
addproject_section.py, поэтому текст и стиль совпадают с блоком в гайде.
"""
import os

_here = os.path.dirname(os.path.abspath(__file__))
_src = open(os.path.join(_here, 'build_guide.py'), encoding='utf-8').read()
_preamble = _src.split('#  ТИТУЛ')[0]          # всё до титульного блока: стили + помощники
_ns = {}
exec(compile(_preamble, 'build_guide_preamble', 'exec'), _ns)

doc = _ns['doc']
h1, h2, h3 = _ns['h1'], _ns['h2'], _ns['h3']
para, bullet, table = _ns['para'], _ns['bullet'], _ns['table']
tip, warn, check = _ns['tip'], _ns['warn'], _ns['check']

from addproject_section import render as render_addproject
render_addproject(h1, h2, h3, para, bullet, table, tip, warn, check)

_out = os.path.join(_here, 'Добавление проекта.docx')
doc.save(_out)
print('OK saved →', _out)
