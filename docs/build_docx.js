// Мини-конвертер Markdown → styled .docx (под стиль гайда OpenGar).
// Поддержка: # ## ### #### заголовки, --- TABLE --- таблицы, - списки,
// **жирный** *курсив* `код`, коллауты 💡/⚠️/✅, метки «Скриншот», <!-- --> комменты.
const fs = require('fs');
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, Table, TableRow, TableCell,
  WidthType, BorderStyle, ShadingType, AlignmentType, LevelFormat, PageBreak,
} = require('docx');

const SRC = process.argv[2];
const OUT = process.argv[3];
const raw = fs.readFileSync(SRC, 'utf8');
const lines = raw.split('\n');

const CONTENT_W = 9020;          // ширина контентной области (A4 − поля), DXA
const C = {
  h1: '1A56E8', h2: '243B7A', h3: '3A5BD9', h4: '5A6B7B',
  tipBg: 'EAF1FF', warnBg: 'FFF3E0', okBg: 'E9F7EC', shotBg: 'F0EFEA',
  thBg: '243B7A', zebra: 'F5F7FB', line: 'C9D3E8',
};

// ── inline: **bold** *italic* `code` → TextRun[] ──
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g;
  let last = 0, m;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const t = m[0];
    if (t.startsWith('**')) out.push(new TextRun({ text: t.slice(2, -2), bold: true, ...base }));
    else if (t.startsWith('`')) out.push(new TextRun({ text: t.slice(1, -1), font: 'Consolas', shading: { type: ShadingType.CLEAR, fill: 'EEEEEE' }, ...base }));
    else out.push(new TextRun({ text: t.slice(1, -1), italics: true, ...base }));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  if (out.length === 0) out.push(new TextRun({ text: '', ...base }));
  return out;
}

function para(text, opts = {}) { return new Paragraph({ children: runs(text), spacing: { after: 120, line: 276 }, ...opts }); }

function callout(text, fill) {
  return new Paragraph({
    children: runs(text),
    spacing: { before: 60, after: 120, line: 276 },
    shading: { type: ShadingType.CLEAR, fill },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: 'B9C7E8', space: 8 } },
    indent: { left: 120, right: 120 },
  });
}

function screenshot(text) {
  return new Paragraph({
    children: [new TextRun({ text: '📷  ' + text.replace(/\*\*/g, ''), italics: true, color: '8A857A' })],
    spacing: { before: 80, after: 140 },
    shading: { type: ShadingType.CLEAR, fill: C.shotBg },
    alignment: AlignmentType.CENTER,
    border: {
      top: { style: BorderStyle.DASHED, size: 6, color: 'C9C4B8' },
      bottom: { style: BorderStyle.DASHED, size: 6, color: 'C9C4B8' },
      left: { style: BorderStyle.DASHED, size: 6, color: 'C9C4B8' },
      right: { style: BorderStyle.DASHED, size: 6, color: 'C9C4B8' },
    },
  });
}

function bullet(text) {
  return new Paragraph({ children: runs(text), bullet: { level: 0 }, spacing: { after: 60, line: 276 } });
}

function heading(text, level) {
  const map = { 1: HeadingLevel.HEADING_1, 2: HeadingLevel.HEADING_2, 3: HeadingLevel.HEADING_3, 4: HeadingLevel.HEADING_4 };
  const col = { 1: C.h1, 2: C.h2, 3: C.h3, 4: C.h4 }[level];
  const sz = { 1: 34, 2: 28, 3: 24, 4: 21 }[level];
  const p = { 1: 320, 2: 260, 3: 200, 4: 160 }[level];
  return new Paragraph({
    heading: map[level],
    spacing: { before: p, after: 100 },
    border: level <= 1 ? { bottom: { style: BorderStyle.SINGLE, size: 12, color: col, space: 4 } } : undefined,
    children: [new TextRun({ text, bold: true, color: col, size: sz })],
  });
}

function table(rows) {
  const cols = Math.max(...rows.map(r => r.length));
  const colW = Math.floor(CONTENT_W / cols);
  const widths = Array(cols).fill(colW);
  const trs = rows.map((cells, ri) => new TableRow({
    tableHeader: ri === 0,
    children: Array.from({ length: cols }, (_, ci) => {
      const val = cells[ci] || '';
      const isH = ri === 0;
      return new TableCell({
        width: { size: colW, type: WidthType.DXA },
        shading: { type: ShadingType.CLEAR, fill: isH ? C.thBg : (ri % 2 === 0 ? C.zebra : 'FFFFFF') },
        margins: { top: 40, bottom: 40, left: 90, right: 90 },
        children: [new Paragraph({
          spacing: { after: 0, line: 260 },
          children: isH ? [new TextRun({ text: val, bold: true, color: 'FFFFFF', size: 19 })]
                        : runs(val, { size: 19 }),
        })],
      });
    }),
  }));
  return new Table({
    columnWidths: widths,
    width: { size: CONTENT_W, type: WidthType.DXA },
    borders: {
      top: { style: BorderStyle.SINGLE, size: 4, color: C.line },
      bottom: { style: BorderStyle.SINGLE, size: 4, color: C.line },
      left: { style: BorderStyle.SINGLE, size: 4, color: C.line },
      right: { style: BorderStyle.SINGLE, size: 4, color: C.line },
      insideHorizontal: { style: BorderStyle.SINGLE, size: 4, color: C.line },
      insideVertical: { style: BorderStyle.SINGLE, size: 4, color: C.line },
    },
    rows: trs,
  });
}

// ── парсинг ──
const kids = [];
let i = 0;
while (i < lines.length) {
  let ln = lines[i];
  const t = ln.trim();
  if (t.startsWith('<!--')) { while (i < lines.length && !lines[i].includes('-->')) i++; i++; continue; }
  if (t === '') { i++; continue; }
  if (t === '---') { kids.push(new Paragraph({ border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: C.line } }, spacing: { before: 80, after: 80 } })); i++; continue; }
  if (t === '--- TABLE ---') {
    const rows = []; i++;
    while (i < lines.length && lines[i].trim() !== '--- /TABLE ---') {
      rows.push(lines[i].split('|').map(s => s.trim())); i++;
    }
    i++; kids.push(table(rows)); kids.push(new Paragraph({ spacing: { after: 80 } })); continue;
  }
  let m;
  if ((m = t.match(/^(#{1,4})\s+(.*)$/))) { kids.push(heading(m[2], m[1].length)); i++; continue; }
  if (/^\*\*Скриншот|^\*\*Скрин/.test(t) || /^Скриншот\b/.test(t)) { kids.push(screenshot(t)); i++; continue; }
  if (t.startsWith('- ')) { kids.push(bullet(t.slice(2))); i++; continue; }
  if (/^(💡|⚠️|✅)/.test(t)) {
    const fill = t.startsWith('💡') ? C.tipBg : t.startsWith('⚠️') ? C.warnBg : C.okBg;
    kids.push(callout(t, fill)); i++; continue;
  }
  kids.push(para(t)); i++;
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: 'Calibri', size: 21 } } },
    paragraphStyles: [
      { id: 'Heading1', name: 'Heading 1', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { bold: true } },
      { id: 'Heading2', name: 'Heading 2', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { bold: true } },
      { id: 'Heading3', name: 'Heading 3', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { bold: true } },
      { id: 'Heading4', name: 'Heading 4', basedOn: 'Normal', next: 'Normal', quickFormat: true, run: { bold: true } },
    ],
  },
  numbering: {
    config: [{
      reference: 'b', levels: [{ level: 0, format: LevelFormat.BULLET, text: '•', alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 340, hanging: 200 } } } }],
    }],
  },
  sections: [{
    properties: { page: { margin: { top: 1200, bottom: 1200, left: 1300, right: 1300 } } },
    children: kids,
  }],
});

Packer.toBuffer(doc).then(buf => { fs.writeFileSync(OUT, buf); console.log('OK', OUT, buf.length, 'bytes'); });
